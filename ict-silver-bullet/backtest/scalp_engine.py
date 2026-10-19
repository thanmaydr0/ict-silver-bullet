"""Offline bar replay with next-open fills and floating risk inside the loop.

One portfolio position at a time. Intrabar path is open -> adverse (stop first)
-> favourable -> close. This is an assumption; M1 cannot certify tick drawdown.
"""
from collections import Counter, defaultdict
from datetime import timedelta
from math import floor, isfinite
from backtest.scalp_policy import Costs, Limits, aware, market_open, fx_day
from backtest.scalp_signals import Detector, ICTDetector
from brain.news.blackout import is_in_news_blackout


def run(bars, spec, *, costs=None, risk=.0005, initial=10000., start=None,
        end=None, news_events=(), news_complete=False, candidate=None):
    costs = costs or Costs()
    if not isfinite(initial) or initial <= 0:
        raise ValueError('positive finite capital required')
    if not 0 < risk <= .005:
        raise ValueError('invalid risk')
    start = aware(start) if start else None
    end = aware(end) if end else None
    state = Limits(balance=initial, peak=initial, day_balance=initial)
    models = defaultdict(lambda: ICTDetector() if spec.family=='ict_baseline' else Detector(spec))
    pending, position = {}, None
    trades, marks, ledger = [], [], []
    reasons, counts = Counter(), Counter()
    last_seen, previous_global = {}, None
    observed_months = set()
    risk_clock, busy_until = None, None

    def sample(now, equity):
        # M1 bounds have no known intrabar timestamp. Assign all bounds in a
        # minute to its close; both symbols share this risk observation clock.
        clock = risk_clock or now
        flags = state.mark(equity, clock)
        if position:
            base = state.balance+position['entry_commission']
            position['path_r'].append((equity-base)/position['risk_usd'])
        stamp = clock.isoformat()
        if (not position and marks and marks[-1]['equity'] == equity
                and marks[-1]['balance'] == state.balance
                and marks[-1]['fx_day'] == str(fx_day(clock))
                and marks[-1]['ts'][:13] == stamp[:13]):
            return flags
        marks.append(dict(ts=stamp, equity=equity, balance=state.balance,
                          fx_day=str(fx_day(clock)), **flags))
        return flags

    def close(bid, spread, now, reason):
        nonlocal position, busy_until
        p = position
        quote = bid + (spread*.0001 if p['sign'] < 0 else 0)
        fill = quote-p['sign']*costs.slip()*.0001
        gross_ex_cost = (bid-p['entry_bid'])*p['sign']*p['units']
        spread_paid = p['spread_paid']+(spread*10*p['lots'] if p['sign'] < 0 else 0)
        slip_paid = (costs.slip(True)+costs.slip())*10*p['lots']
        commission = costs.commission*p['lots']
        net = (fill-p['entry'])*p['sign']*p['units']-commission
        state.balance += net+p['entry_commission']
        ledger.append(dict(ts=now.isoformat(), amount=commission/2, kind='commission'))
        ledger.append(dict(ts=now.isoformat(), amount=(spread*10*p['lots'] if p['sign'] < 0 else 0)
                           +costs.slip()*10*p['lots'], kind='spread_slippage'))
        trades.append(dict(symbol=p['symbol'], opened_at=p['opened'].isoformat(),
            lots=p['lots'],
            closed_at=now.isoformat(), net=net, gross=gross_ex_cost, costs=spread_paid+slip_paid+commission,
            spread_cost=spread_paid, slippage_cost=slip_paid, commission=commission,
            risk_usd=p['risk_usd'], r=net/p['risk_usd'], reason=reason, regime=p['regime'],
            minimum_r=p['minimum_pnl']/p['risk_usd'], maximum_r=p['maximum_pnl']/p['risk_usd']))
        trades[-1]['path_r'] = p['path_r']+[net/p['risk_usd']]
        position = None
        busy_until = now
        sample(now, state.balance)

    for raw in bars:
        row = dict(raw)
        now, symbol = aware(row['ts']), row['pair']
        if previous_global and now < previous_global:
            raise ValueError('bars must be globally chronological')
        if symbol in last_seen and now <= last_seen[symbol]:
            raise ValueError('duplicate or out-of-order symbol bar')
        previous_global, last_seen[symbol] = now, now
        for k in ('open', 'high', 'low', 'close'):
            if not isfinite(row[k]) or row[k] <= 0:
                raise ValueError('positive finite OHLC required')
        if not row['low'] <= min(row['open'],row['close']) <= max(row['open'],row['close']) <= row['high']:
            raise ValueError('invalid OHLC geometry')
        decision_time = now+timedelta(minutes=1)
        risk_clock = decision_time
        if end and now >= end:
            break
        signal = candidate(row) if candidate else models[symbol].update(row)
        if start and now < start:
            continue  # feature warmup only; no positions, no account activity
        observed_months.add(now.strftime('%Y-%m'))
        sample(now, state.balance if not position else marks[-1]['equity'])
        blocked = is_in_news_blackout(now, symbol, news_events)
        spread = costs.spread(symbol, now, row.get('spread_pips'), blocked)
        p = pending.pop(symbol, None)
        if p and now != p['decision_at']:
            reasons['missing_next_bar'] += 1
            p = None
        if position and position['symbol'] == symbol:
            if (not market_open(now) or blocked or
                    now-position['opened'] >= timedelta(minutes=spec.hold_minutes)):
                close(row['open'], spread, now, 'session_news_time_exit')
        if p and not position and market_open(now) and not blocked and (not busy_until or busy_until <= now):
            flags = sample(now, state.balance)
            cash_risk = state.budget(risk, state.balance)
            entry = row['open']+(spread*.0001 if p['sign'] > 0 else 0)+p['sign']*costs.slip(True)*.0001
            distance = (entry-p['stop'])*p['sign']
            # Include anticipated exit commission/slippage in the loss budget.
            loss_per_lot = distance*100000+costs.commission+costs.slip()*10
            lots = floor(cash_risk/loss_per_lot*100)/100 if distance > 0 else 0
            if counts[fx_day(now)] >= 8:
                reasons['daily_trade_cap'] += 1
            elif (lots > 0 and (p['target']-entry)*p['sign'] > 0
                  and ('reference_entry' not in p or abs(entry-p['reference_entry']) <= .0002)
                  and not state.daily_halt and not state.overall_halt):
                fee = lots*costs.commission/2
                state.balance -= fee
                position = dict(p, symbol=symbol, lots=lots, units=100000*lots, entry=entry,
                    entry_bid=row['open'], opened=now, risk_usd=lots*loss_per_lot,
                    spread_paid=spread*10*lots if p['sign'] > 0 else 0,
                    entry_commission=fee, minimum_pnl=-fee, maximum_pnl=-fee)
                position['path_r'] = []
                counts[fx_day(now)] += 1
                ledger.append(dict(ts=now.isoformat(), amount=fee, kind='commission'))
                ledger.append(dict(ts=now.isoformat(), amount=(spread if p['sign'] > 0 else 0)*10*lots
                                   +costs.slip(True)*10*lots, kind='spread_slippage'))
            else:
                reasons['risk_or_geometry_rejection'] += 1
        if position and position['symbol'] == symbol:
            p = position
            extra = spread*.0001 if p['sign'] < 0 else 0
            opening = row['open']+extra
            adverse = (row['low'] if p['sign'] > 0 else row['high'])+extra
            favourable = (row['high'] if p['sign'] > 0 else row['low'])+extra
            stopped = (adverse-p['stop'])*p['sign'] <= 0
            exit_adverse = (min(opening,p['stop']) if p['sign'] > 0 else max(opening,p['stop'])) if stopped else adverse
            def floating(quote):
                return (quote-p['entry'])*p['sign']*p['units']-p['entry_commission']-costs.commission*p['lots']/2-costs.slip()*10*p['lots']
            worst = floating(exit_adverse)
            p['minimum_pnl'] = min(p['minimum_pnl'], worst)
            flags = sample(now, state.balance+p['entry_commission']+worst)
            if stopped:
                close(exit_adverse-extra, spread, decision_time, 'stop')
            elif state.daily_halt or state.overall_halt:
                close(adverse-extra, spread, decision_time, 'risk_flatten')
            elif (favourable-p['target'])*p['sign'] >= 0:
                p['maximum_pnl'] = max(p['maximum_pnl'],floating(p['target']))
                close(p['target']-extra,spread,decision_time,'target')
            else:
                p['maximum_pnl'] = max(p['maximum_pnl'],floating(favourable))
                sample(now,state.balance+p['entry_commission']+floating(favourable))
                sample(decision_time,state.balance+p['entry_commission']+floating(row['close']+extra))
        if signal and market_open(decision_time) and not is_in_news_blackout(decision_time,symbol,news_events):
            if not state.daily_halt and not state.overall_halt and not position:
                pending[symbol] = signal
        elif blocked:
            reasons['news_blackout'] += 1
    if position:
        # NOTE: Never close at a future bar or fabricate an end-boundary quote.
        reasons['unclosed_at_boundary'] += 1
    return dict(trades=trades, marks=marks, costs_ledger=ledger, initial=initial,
                final_balance=state.balance, open_position=position is not None,
                months=sorted(observed_months), reasons=dict(reasons),
                news_complete=news_complete, spread_source='recorded pips when present; otherwise MODELLED',
                intrabar_model='open/adverse/favourable/close; stop-first; floating costs included',
                latency_model=f'{costs.latency_seconds}s assumed; next-open plus adverse pip allowance',
                live_eligible=False)
