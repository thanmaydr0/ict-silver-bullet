"""Retrospective monthly accounting and calendar-day block bootstrap.

Bootstrap retains observed intratrade bound paths; no simulated price series.
The observed opportunity set is conditional, not a full strategy-path model.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import sqrt, floor
import random
from statistics import NormalDist, mean, median, pstdev
from backtest.scalp_policy import Limits, aware, fx_day


def metrics(trades):
    wins = [t['net'] for t in trades if t['net'] > 0]
    losses = [-t['net'] for t in trades if t['net'] < 0]
    return dict(trades=len(trades), win_rate=100*len(wins)/len(trades) if trades else 0.,
                winning_trades=len(wins),losing_trades=len(losses),
                average_r=mean(t['r'] for t in trades) if trades else None,
                expectancy=mean(t['net'] for t in trades) if trades else None,
                profit_factor=sum(wins)/sum(losses) if losses else None,
                profit_factor_status='finite' if losses else 'no_losses' if wins else 'no_profit_or_loss',
                net_profit=sum(t['net'] for t in trades))


def monthly(report, tags=None):
    rows, starting = [], report['initial']
    for month in report['months']:
        trades = [t for t in report['trades'] if t['closed_at'].startswith(month)]
        marks = [m for m in report['marks'] if m['ts'].startswith(month)]
        balance = marks[-1]['balance'] if marks else starting
        by_day = defaultdict(list)
        for m in marks:
            by_day[m['fx_day']].append(m)
        intraday_dd = 0.
        days = []
        for ms in by_day.values():
            peak = ms[0]['equity']
            day_start_balance = ms[0]['balance']
            for m in ms:
                peak = max(peak,m['equity'])
                intraday_dd = max(intraday_dd,1-m['equity']/peak)
            days.append(100*(ms[-1]['balance']/day_start_balance-1))
        streak = longest = 0
        for t in trades:
            streak = streak+1 if t['net'] < 0 else 0
            longest = max(longest,streak)
        cost = sum(x['amount'] for x in report['costs_ledger'] if x['ts'].startswith(month))
        rows.append(dict(month=month, tag=(tags or {}).get(month,'RETROSPECTIVE'),
            symbols=','.join(sorted({t['symbol'] for t in trades})), **metrics(trades),
            gross_profit=sum(max(0,t['gross']) for t in trades),
            gross_loss=sum(min(0,t['gross']) for t in trades), costs_paid=cost,
            starting_balance=starting, ending_balance=balance,
            net_return_pct=100*(balance/starting-1), max_intraday_dd_pct=100*intraday_dd,
            worst_daily_dd_pct=100*max((m['daily_dd'] for m in marks),default=0),
            max_overall_dd_pct=100*max((m['overall_dd'] for m in marks),default=0),
            soft_breaker_days=len({m['fx_day'] for m in marks if m['soft_daily'] or m['soft_overall']}),
            hard_breaker_days=len({m['fx_day'] for m in marks if m['hard_daily'] or m['hard_overall']}),
            best_day_pct=max(days,default=0), worst_day_pct=min(days,default=0),
            longest_losing_streak=longest, hit_10pct=balance >= starting*1.1,
            breach_daily=any(m['breach_daily'] for m in marks),
            breach_overall=any(m['breach_overall'] for m in marks),
            spread_source=report['spread_source']))
        starting = balance
    return rows


def summary(rows):
    if not rows:
        return dict(months=0)
    losing = longest = 0
    for r in rows:
        losing = losing+1 if r['net_return_pct'] < 0 else 0
        longest = max(longest,losing)
    return dict(months=len(rows), hit_10_count=sum(r['hit_10pct'] for r in rows),
        hit_10_pct=100*sum(r['hit_10pct'] for r in rows)/len(rows),
        profitable_count=sum(r['net_return_pct']>0 for r in rows),
        profitable_pct=100*sum(r['net_return_pct']>0 for r in rows)/len(rows),
        losing_count=sum(r['net_return_pct']<0 for r in rows),
        losing_pct=100*sum(r['net_return_pct']<0 for r in rows)/len(rows),
        breach_count=sum(r['breach_daily'] or r['breach_overall'] for r in rows),
        breach_pct=100*sum(r['breach_daily'] or r['breach_overall'] for r in rows)/len(rows),
        longest_losing_month_run=longest,
        best_month=max(rows,key=lambda r:r['net_return_pct'])['month'],
        worst_month=min(rows,key=lambda r:r['net_return_pct'])['month'])


def quantile(xs,p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs)-1)*p
    lo = int(k)
    return xs[lo]+(xs[min(lo+1,len(xs)-1)]-xs[lo])*(k-lo)


def kelly_cap(trades, *, samples=1000, seed=1701):
    """Quarter-Kelly small-fraction bound using a day-cluster mean haircut."""
    days = defaultdict(list)
    for t in trades:
        days[str(fx_day(aware(t['closed_at'])))].append(t['r'])
    if len(days) < 20:
        return dict(cap=0., reason='fewer_than_20_observed_days')
    blocks, rng = list(days.values()), random.Random(seed)
    means = []
    for _ in range(samples):
        draw = [x for b in rng.choices(blocks,k=len(blocks)) for x in b]
        means.append(mean(draw))
    lower = quantile(means,.025)
    second = mean(t['r']**2 for t in trades)
    return dict(cap=min(.005,max(0.,lower)/(4*second)) if second else 0.,
                lower_95_expectancy_r=lower, fraction=.25,
                method='day-cluster bootstrap lower mean / second moment; small-fraction approximation')


def bootstrap(trades, *, risk, first_day, last_day, simulations=1000,
              block_days=5, seed=1701):
    """Resample consecutive calendar-day blocks; evaluate all 30 days.

    Reapply breakers to every observed intratrade equity bound, retain empty
    days, and never terminate a path merely because it briefly reached +10%.
    This conditions on observed completed trades; it is not a gap-tail model.
    """
    if simulations < 1 or block_days < 1 or not 0 < risk <= .005:
        raise ValueError('invalid simulation parameters')
    # Bounds are [start,end); use the FX-day labels actually observed, not UTC
    # dates that would silently discard the first morning's trades.
    first_day = fx_day(aware(first_day))
    last_day = fx_day(aware(last_day)-timedelta(microseconds=1))
    calendar = [[] for _ in range((last_day-first_day).days+1)]
    for t in trades:
        if not t.get('path_r'):
            raise ValueError('floating path is required; closed-R permutations are insufficient')
        offset = (fx_day(aware(t['closed_at']))-first_day).days
        if 0 <= offset < len(calendar):
            calendar[offset].append(t)
    if len(calendar) < block_days:
        raise ValueError('insufficient calendar history')
    rng, returns, drawdowns = random.Random(seed), [], []
    daily_breaches = overall_breaches = union_breaches = 0
    for _ in range(simulations):
        sequence = []
        while len(sequence) < 30:
            i = rng.randrange(len(calendar)-block_days+1)
            sequence.extend(calendar[i:i+block_days])
        state, breached_daily, breached_overall, maximum = Limits(),False,False,0.
        for j, day_trades in enumerate(sequence[:30]):
            now = datetime(2020,1,1,18,tzinfo=timezone.utc)+timedelta(days=j)
            state.new_day(now)
            for trade in day_trades:
                cash = state.budget(risk,state.balance)
                if trade.get('lots'):
                    unit_risk = trade['risk_usd']/trade['lots']
                    cash = floor(cash/unit_risk*100)/100*unit_risk
                if cash <= 0:
                    continue
                base, exit_r = state.balance, trade['r']
                for value in trade['path_r']:
                    flags = state.mark(base+cash*value,now)
                    maximum = max(maximum,flags['overall_dd'])
                    breached_daily |= flags['breach_daily']
                    breached_overall |= flags['breach_overall']
                    if state.daily_halt or state.overall_halt:
                        exit_r = value
                        break
                state.balance = base+cash*exit_r
                state.mark(state.balance,now)
        returns.append(100*(state.balance/10000-1))
        drawdowns.append(100*maximum)
        daily_breaches += breached_daily
        overall_breaches += breached_overall
        union_breaches += breached_daily or breached_overall
    return dict(simulations=simulations, block_days=block_days, seed=seed, risk=risk,
        p_10pct=sum(x>=10 for x in returns)/simulations,
        p_daily_breach=daily_breaches/simulations, p_overall_breach=overall_breaches/simulations,
        p_union_breach=union_breaches/simulations,
        p_union_breach_upper95=wilson_upper(union_breaches,simulations),
        monthly_return_pct=dict(p05=quantile(returns,.05),median=quantile(returns,.5),p95=quantile(returns,.95)),
        max_dd_pct=dict(p05=quantile(drawdowns,.05),median=quantile(drawdowns,.5),p95=quantile(drawdowns,.95)),
        limitation='conditional observed M1 bound paths, missing tail events and opportunity changes are not identified')


def wilson_upper(successes,n):
    z=NormalDist().inv_cdf(.95)
    p=successes/n
    return (p+z*z/(2*n)+z*sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)


def numerical_gates(candidate):
    base,stress=candidate['metrics'],candidate['stress']
    months=candidate['months']
    total=sum(r['net_profit'] for r in months)
    frontier=[x for x in candidate['frontier'] if x['kelly_permitted']]
    return dict(trades_300=base['trades']>=300,
        months_12=sum(r['trades']>0 for r in months)>=12,
        regime_coverage=all(x['trades']>0 for x in candidate['regimes'].values()),
        net_expectancy=(base['average_r'] or 0)>0,
        stressed_expectancy=(stress['average_r'] or 0)>0,
        profit_factor=(base['profit_factor'] or 0)>=1.3 or base.get('profit_factor_status')=='no_losses',
        month_concentration=total>0 and max((r['net_profit'] for r in months),default=0)/total<=.4,
        monte_carlo=any(x['p_union_breach_upper95']<=.05 for x in frontier),
        original_untouched_lockbox=False,source_clock_verified=False,historical_news_complete=False,
        parameter_robustness='requires sensitivity review; no automatic sharp-peak approval')


def robustness(cells,stop,reward):
    """Predeclared conservative rejection rule; never a retuning mechanism."""
    center=next(x for x in cells if x['stop_atr']==stop and x['reward']==reward)
    neighbors=[x for x in cells if x is not center]
    values=[x['average_r'] for x in neighbors]
    stressed=[x['stress']['average_r'] for x in neighbors]
    complete=len(neighbors)==8 and all(x is not None for x in values+stressed)
    passed=bool(complete and (center['average_r'] or 0)>0 and min(values)>0
                and median(values)>=.5*center['average_r'] and min(stressed)>0)
    return dict(passed=passed,rule='all eight neighbors positive normally and under stress; median neighbor >=50% of positive center',
                center_expectancy_r=center['average_r'],neighbor_median_r=median(values) if complete else None,
                limitation='predeclared diagnostic thresholds, not an independent significance test')


def multiplicity_haircut(returns, trials):
    """Bonferroni one-sided normal-mean test: an explicit equivalent haircut.

    Monthly sample is small and may be dependent. This is a diagnostic, not a
    deflated-Sharpe certification; unknown previous trials invalidate a pass.
    """
    if len(returns) < 12 or trials < 1 or not pstdev(returns):
        return dict(adjusted_p=None, reason='insufficient_nonconstant_months')
    z = mean(returns)/(pstdev(returns)/sqrt(len(returns)))
    return dict(z=z, adjusted_p=min(1.,trials*(1-NormalDist().cdf(z))),
                declared_trials=trials, prior_trials='unknown', live_eligible=False)
