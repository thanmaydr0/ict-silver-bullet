"""Fixed-logic diagnostics on existing prices; no strategy selection or promotion.

All R in diagnostic tables uses the signal's original price-stop distance.
The earlier study's fee-inclusive R is also retained as engine_average_r.
"""
import argparse
from collections import defaultdict, Counter
from dataclasses import dataclass, replace, asdict
from datetime import timedelta
import csv
import gzip
import hashlib
import json
from pathlib import Path
import random
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

from backtest.scalp_policy import Costs, NY, aware, market_open
from backtest.scalp_signals import Detector, ICTDetector, Specification, FAMILIES
from backtest.scalp_engine import run
from backtest.scalp_study import read_data, stream, write_run, write_json, trial, finish_trial
from backtest.scalp_analysis import metrics, monthly, quantile
from brain.logging_setup import setup_logging


@dataclass(frozen=True)
class DiagnosticCosts(Costs):
    mode: str = 'full'

    def spread(self, symbol, now, recorded=None, news=False):
        return 0. if self.mode=='zero' else super().spread(symbol,now,recorded,news)

    def slip(self, entry=False):
        return super().slip(entry) if self.mode=='full' else 0.

    @property
    def commission(self):
        return super().commission if self.mode in ('full','spread_commission') else 0.


def describe(report):
    ts=report['trades']
    def avg(key):
        return mean(t[key]/t['reference_r_usd'] for t in ts) if ts else None
    stops=mean(t['stop_distance_pips'] for t in ts) if ts else None
    cp=mean(t['costs']/(10*t['lots']) for t in ts) if ts else None
    barrier=[t for t in ts if t['reason'] in ('stop','target','target_ambiguous')]
    return dict(trades=len(ts),gross_r=avg('gross'),net_r=avg('net'),cost_r=avg('costs'),
        fixed_spread_only_r=mean((t['gross']-t['spread_cost'])/t['reference_r_usd'] for t in ts) if ts else None,
        fixed_spread_commission_r=mean((t['gross']-t['spread_cost']-t['commission'])/t['reference_r_usd'] for t in ts) if ts else None,
        average_stop_pips=stops,average_cost_pips=cp,cost_over_average_stop=cp/stops if stops else None,
        engine_average_r=metrics(ts)['average_r'],profit_factor=metrics(ts)['profit_factor'],
        win_pct=metrics(ts)['win_rate'],barrier_trades=len(barrier),
        barrier_win_pct=100*sum(t['net']>0 for t in barrier)/len(barrier) if barrier else None,
        ambiguous_trades=sum(t['ambiguous'] for t in ts),
        ambiguous_share_pct=100*sum(t['ambiguous'] for t in ts)/len(ts) if ts else 0.,
        ambiguous_bars=report['reasons'].get('both_touched',0),
        opening_target_then_stop=report['reasons'].get('open_already_beyond_target_then_stop',0),
        accounting_max_error=max((abs(t['gross']-t['costs']-t['net']) for t in ts),default=0.),
        risk_flags_seen=';'.join(report['risk_flags_seen']),overall_halt=report['overall_halt'],
        open_position=report['open_position'])


def tape(frames,spec,minutes=1,stop_scale=1.):
    """Cache causal signals once; only completed contiguous aggregate bars exist.

    Higher-TF indicator lookbacks keep their bar counts. Sessions, cooldown and
    60-minute holding cap keep wall-clock durations. Stops use 3x/6x the M1
    ATR/five-pip rule known before decision, rather than tuning their width.
    """
    import numpy as np
    result={}
    for symbol,frame in frames.items():
        model=ICTDetector() if spec.family=='ict_baseline' else Detector(spec,minutes)
        bucket=[]; previous=None; trs=[]; previous_close=None
        for row in stream({symbol:frame}):
            ts=row['ts']
            contiguous=previous is None or ts==previous+timedelta(minutes=1)
            if not contiguous:
                bucket=[]; trs=[]; previous_close=None
            prior_atr=sum(trs[-20:])/20 if minutes>1 and len(trs)>=20 else None
            if minutes>1:
                tr=max(row['high']-row['low'],abs(row['high']-previous_close),abs(row['low']-previous_close)) if previous_close is not None else row['high']-row['low']
                trs.append(tr)
                if len(trs)>21: trs.pop(0)
            previous_close=row['close']; previous=ts
            if minutes==1:
                signal=model.update(row)
            else:
                boundary=ts.replace(minute=(ts.minute//minutes)*minutes,second=0,microsecond=0)
                if bucket and bucket[0]['ts']!=boundary:
                    bucket=[]
                if not bucket and ts!=boundary:
                    continue
                bucket.append(row)
                if len(bucket)!=minutes:
                    continue
                bar=dict(row,ts=boundary,open=bucket[0]['open'],
                    high=max(b['high'] for b in bucket),low=min(b['low'] for b in bucket))
                signal=model.update(bar);bucket=[]
            if signal:
                signal=dict(signal)
                reference=signal.get('reference_entry',row['close'])
                distance=abs(reference-signal['stop'])
                if minutes>1:
                    if prior_atr is None: continue
                    distance=stop_scale*max(spec.min_stop_pips*.0001,spec.stop_atr*prior_atr)
                    signal.update(stop=reference-signal['sign']*distance,
                                  target=reference+signal['sign']*spec.reward*distance)
                signal['reference_distance']=distance
                result[(ts,symbol)]=signal
    return result


def sparse_index(frames):
    result={}
    for symbol,frame in frames.items():
        dates=frame.ts.dt.strftime('%Y-%m-%d')
        endpoints=(~dates.duplicated(keep='first') | ~dates.duplicated(keep='last')).to_numpy()
        result[symbol]=(frame.ts.dt.as_unit('ns').astype('int64').to_numpy(),endpoints)
    return result


def sparse_frames(frames,signals,hold=60,index_cache=None):
    """Drop only flat/no-signal intervals. Retain every M1 bar of any possible
    position and the first quote after its holding deadline, plus day endpoints.
    No prices are interpolated; candidate already contains its warm indicators.
    """
    import numpy as np
    out={};index_cache=index_cache or sparse_index(frames)
    for symbol,frame in frames.items():
        index,endpoints=index_cache[symbol]
        times=np.array([int(ts.timestamp())*10**9 for ts,pair in signals if pair==symbol],dtype='int64')
        starts=np.searchsorted(index,times)
        ends=np.minimum(len(frame),np.searchsorted(index,times+(hold+1)*60*10**9)+1)
        events=np.zeros(len(frame)+1,dtype='int32')
        np.add.at(events,starts,1);np.add.at(events,ends,-1)
        mask=(np.cumsum(events[:-1])>0) | endpoints
        out[symbol]=frame.loc[mask]
    return out


def event_cache(frames,indexed):
    return {symbol:dict(index=indexed[symbol][0],endpoints=indexed[symbol][1],
        columns={col:frame[col].array if col=='ts' else frame[col].to_numpy() for col in frame})
        for symbol,frame in frames.items()}


def event_stream(cache,signals,activity):
    """Seek through flat intervals, then yield EVERY original M1 bar while an
    order is pending or a position is open. The existing engine controls seeking.
    Daily endpoints preserve zero-trade calendar accounting. No outcome lookup,
    alternative execution kernel or interpolated data is used.
    """
    import numpy as np
    events=[]; cursors={symbol:0 for symbol in cache}
    for symbol,c in cache.items():
        times=np.array([int(ts.timestamp())*10**9 for ts,pair in signals if pair==symbol],dtype='int64')
        indices=np.unique(np.concatenate((np.searchsorted(c['index'],times),np.flatnonzero(c['endpoints']))))
        events.extend((int(c['index'][i]),symbol,int(i)) for i in indices if i<len(c['index']))
    events.sort(); event_cursor=0;previous=(-1,'')
    while True:
        while event_cursor<len(events) and events[event_cursor][:2]<=previous:
            event_cursor+=1
        if activity.get('active'):
            choices=[(int(c['index'][cursors[symbol]]),symbol,cursors[symbol])
                     for symbol,c in cache.items() if cursors[symbol]<len(c['index'])]
            if not choices: return
            stamp,symbol,i=min(choices)
        else:
            if event_cursor==len(events): return
            stamp,symbol,i=events[event_cursor]
            for pair,c in cache.items():
                k=int(np.searchsorted(c['index'],stamp))
                if pair<symbol and k<len(c['index']) and c['index'][k]==stamp: k+=1
                cursors[pair]=max(cursors[pair],k)
        cursors[symbol]=i+1;previous=(stamp,symbol)
        row={col:values[i] for col,values in cache[symbol]['columns'].items()}
        row={col:value.item() if isinstance(value,np.generic) else value for col,value in row.items()}
        row['ts']=row['ts'].to_pydatetime()
        yield row


def random_pool(frames):
    """Uniform times/pairs within each actual London/NY decision session.
    Same causal prior ATR20, five-pip floor, 120-bar contiguous warmup.
    """
    import numpy as np
    import pandas as pd
    pool=defaultdict(list)
    for symbol,f in frames.items():
        ts=f.ts
        previous=f.close.shift(1)
        tr=np.maximum(f.high-f.low,np.maximum(abs(f.high-previous),abs(f.low-previous)))
        atr=tr.shift(1).rolling(20).mean()
        contiguous=(ts.diff().dt.total_seconds().fillna(0)==60).rolling(120).sum()==120
        decisions=ts+timedelta(minutes=1)
        for name,zone in (('london','Europe/London'),('ny','America/New_York')):
            local=decisions.dt.tz_convert(zone)
            minute=local.dt.hour*60+local.dt.minute
            keep=contiguous & (minute>=8*60+15) & (minute<10*60) & (local.dt.weekday<5)
            selected=pd.DataFrame(dict(ts=ts,close=f.close,atr=atr,day=local.dt.date)).loc[keep]
            for stamp,close,prior_atr,day in selected.itertuples(index=False,name=None):
                observed=stamp.to_pydatetime()
                now=observed+timedelta(minutes=1)
                if not market_open(now): continue
                pool[(name,str(day))].append((observed,symbol,float(close),max(.0005,1.5*float(prior_atr))))
    return pool


def random_tape(pool,seed,reward):
    rng=random.Random(seed); result={}
    for key in sorted(pool):
        ts,symbol,c,distance=rng.choice(pool[key]); sign=rng.choice((-1,1))
        result[(ts,symbol)]=dict(sign=sign,stop=c-sign*distance,target=c+sign*reward*distance,
            decision_at=ts+timedelta(minutes=1),reference_distance=distance,
            family='random_control',regime='random')
    return result


def table(path,rows):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    fields=list(dict.fromkeys(k for row in rows for k in row))
    with path.with_suffix('.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
    lines=['| '+' | '.join(fields)+' |','| '+' | '.join('---' for _ in fields)+' |']
    for row in rows:
        lines.append('| '+' | '.join('—' if row.get(k,'') is None else str(row.get(k,'')) for k in fields)+' |')
    path.with_suffix('.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--output',type=Path,default=Path('reports/diagnostics'))
    parser.add_argument('--seeds',type=int,default=200)
    args=parser.parse_args(); log=setup_logging('scalp-diagnostics')
    root=Path(__file__).resolve().parents[1]; output=args.output;output.mkdir(parents=True,exist_ok=True)
    frames,audit=read_data(args.data_dir,'label_utc')
    write_json(output/'data_audit.json',audit)
    first=min(aware(a['first']) for a in audit);end=max(aware(a['last']) for a in audit)+timedelta(minutes=1)
    ledger=root/'reports/trials_ledger.csv'
    indexed=sparse_index(frames)
    cached=event_cache(frames,indexed)
    protocol=dict(seeds=args.seeds,seed_range=[0,args.seeds-1],risk=.000001,initial=10000000.,
        resolution_account=dict(risk=.0005,initial=10000.),
        r_basis='original signal price-stop distance; excludes all fees',
        null_times='one uniform actual timestamp/pair per London/NY session per trading day; independent uniform direction',
        null_selection='fixed seed list, all results retained, no best seed',
        source_clock='conditional stored UTC labels',news='not recorded; no historical event gate can be certified',
        timeframe_stops={'M5':3,'M15':6},execution_resolution='M1 always',
        sample='entire previously examined history; explicitly authorized diagnostic reuse, no independent evidence',
        source_code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    if (output/'protocol.json').exists():
        previous=json.loads((output/'protocol.json').read_text())
        parameters=lambda p:{k:v for k,v in p.items() if k!='source_code_sha256'}
        if parameters(previous)!=parameters(protocol):
            raise ValueError('diagnostic protocol changed; do not silently resume')
        if previous!=protocol:
            revisions=output/'code_revisions.json'
            history=json.loads(revisions.read_text()) if revisions.exists() else [previous]
            history.append(protocol);write_json(revisions,history)
            log.warning('Diagnostic implementation revision recorded in %s; parameters unchanged',revisions)
    write_json(output/'protocol.json',protocol)

    def execute(name,spec,signals,mode='full',policy='stop_first',charts=True,screening=False):
        folder=output/name; saved=folder/'diagnostic.json'
        if saved.exists(): return json.loads(saved.read_text())
        settings=DiagnosticCosts(mode=mode)
        risk,initial=(.0005,10000.) if screening else (.000001,10000000.)
        entry=trial(folder,spec,settings,'label_utc',first.isoformat(),end.isoformat(),
            'DIAGNOSTIC',ledger,risk=risk,extra=dict(name=name,ambiguity=policy,initial=initial))
        log.info('START %s',name)
        try:
            activity={}
            rep=run(event_stream(cached,signals,activity),spec,costs=settings,risk=risk,initial=initial,
                    start=first,end=end,candidate=lambda r:signals.get((r['ts'],r['pair'])),
                    ambiguity=policy,retain_marks=charts,activity=activity)
            if rep['open_position']: raise ValueError('position remains at end of real data')
            stat=describe(rep)
            if stat['accounting_max_error']>1e-6: raise ValueError('cash accounting identity failed')
            if charts: write_run(folder,rep,{m:'DIAGNOSTIC_REUSED_HISTORY' for m in rep['months']})
            else:
                folder.mkdir(parents=True,exist_ok=True)
                with gzip.open(folder/'trades.jsonl.gz','wt',encoding='utf-8') as f:
                    for trade_row in rep['trades']: f.write(json.dumps(trade_row)+'\n')
            write_json(saved,stat); finish_trial(entry,ledger,'COMPLETE');log.info('COMPLETE %s trades=%s netR=%s',name,stat['trades'],stat['net_r'])
            return stat
        except Exception:
            finish_trial(entry,ledger,'FAILED');raise

    # Fixed last eligible reward from existing tuning manifests; no new tuning.
    specs={}
    for family in FAMILIES:
        walk=json.loads((root/'reports/monthly/study'/family/'walk_forward.json').read_text())
        reward=next((x['selected_reward'] for x in reversed(walk) if x['selected_reward']),1.5)
        specs[family]=Specification(family,reward=reward)
    specs['ict_baseline']=Specification('ict_baseline',reward=2.)
    write_json(output/'fixed_specifications.json',{f:asdict(s) for f,s in specs.items()})
    costs_rows=[]; attribution=[]; ordering=[]; real={}
    for family,spec in specs.items():
        log.info('CACHE SIGNALS %s',family);signals=tape(frames,spec)
        write_json(output/f'{family}_signal_count.json',dict(signals=len(signals),spec=asdict(spec)))
        for mode in ('zero','spread','spread_commission','full'):
            d=execute(f'{family}/{mode}',spec,signals,mode)
            costs_rows.append(dict(family=family,mode=mode,**d));table(output/'cost_reruns',costs_rows)
            if mode=='full':
                real[family]=d
                attribution.append(dict(family=family,trades=d['trades'],gross_r=d['gross_r'],
                    spread_only_r=d['fixed_spread_only_r'],spread_commission_r=d['fixed_spread_commission_r'],
                    full_r=d['net_r'],mean_cost_r=d['cost_r'],cost_over_average_stop=d['cost_over_average_stop']))
                ordering.append(dict(family=family,policy='stop_first',**d))
        for policy in ('target_first','open_direction'):
            ordering.append(dict(family=family,policy=policy,**execute(f'{family}/{policy}',spec,signals,policy=policy)))
        table(output/'gross_net_same_trades',attribution);table(output/'bar_ordering',ordering)
    best=json.loads((root/'reports/frozen_holdout_receipt.json').read_text())['strategy']
    spec=Specification(**best)
    resolution=[]
    for minutes,scale in ((1,1.),(5,3.),(15,6.)):
        signals=tape(frames,spec,minutes,scale)
        for mode in ('zero','full'):
            d=execute(f'resolution/M{minutes}_{mode}',spec,signals,mode,screening=True)
            resolution.append(dict(minutes=minutes,stop_scale=scale,mode=mode,**d))
            table(output/'resolution',resolution)
    spread_rows=[]
    for hour in range(24):
        now=aware('2024-01-03T00:00:00Z').astimezone(NY).replace(hour=hour)
        spread_rows.append(dict(hour_new_york=hour,
            EURUSD_pips=Costs().spread('EURUSD',now),GBPUSD_pips=Costs().spread('GBPUSD',now),
            news_multiplier=2,recorded_spread='absent in original M1 files',
            recorded_comparison='unavailable',clock='NY local; DST-aware, source mapping unverified'))
    table(output/'spread_by_hour',spread_rows)
    pool=random_pool(frames);null=[]
    rewards=sorted({s.reward for s in specs.values()})
    controls=[(f'{mode}_R{reward:g}',reward,mode) for reward in rewards for mode in ('zero','full')]
    controls.append(('zero_symmetric',1.,'zero'))
    for seed in range(args.seeds):
        for label,reward,mode in controls:
            control=replace(spec,reward=reward)
            signals=random_tape(pool,seed,reward)
            d=execute(f'null/seed_{seed:03d}/{label}',control,signals,mode,charts=False)
            null.append(dict(seed=seed,control=label,**d))
        table(output/'null_seeds',null)
    null_summary=[]
    for label,_,_ in controls:
        rows=[r for r in null if r['control']==label]; xs=[r['net_r'] for r in rows]
        null_summary.append(dict(control=label,seeds=len(rows),mean_r=mean(xs),
            seed_mean_p025=quantile(xs,.025),seed_mean_p975=quantile(xs,.975),
            mean_r_se=pstdev(xs)/(len(xs)**.5),mean_cost_r=mean(r['cost_r'] for r in rows),
            mean_net_plus_cost_r=mean(r['net_r']+r['cost_r'] for r in rows),
            average_win_pct=mean(r['win_pct'] for r in rows),
            average_barrier_win_pct=mean(r['barrier_win_pct'] for r in rows if r['barrier_win_pct'] is not None),
            average_trades=mean(r['trades'] for r in rows)))
    table(output/'null_summary',null_summary)
    comparison=[]
    for family,d in real.items():
        label=f'full_R{specs[family].reward:g}'
        xs=[r['net_r'] for r in null if r['control']==label]
        comparison.append(dict(family=family,real_net_r=d['net_r'],null_mean_r=mean(xs),
            empirical_upper_tail_p=(1+sum(x>=d['net_r'] for x in xs))/(len(xs)+1) if d['net_r'] is not None else None,
            above_null_p975=d['net_r']>quantile(xs,.975) if d['net_r'] is not None else None,
            comparability=f'{label} uniform session null; ICT setup geometry differs'))
    table(output/'real_vs_null',comparison)
    write_json(output/'complete.json',dict(status='COMPLETE',seeds=args.seeds,counts=Counter(r['control'] for r in null)))
    report=['# Existing-engine diagnostic results',
        'Previously examined history; conditional UTC labels; modelled spreads; no historical news calendar. No new strategy or promotion.',
        'R uses original signal price-stop distance. Earlier fee-inclusive R remains in CSV as engine_average_r.',
        'Small diagnostic risk (0.0001%, initial $10,000,000; about $10 loss budget per trade) limits lifetime risk censoring; not a capital or risk recommendation. The resolution-account monthly tables use the original $10,000 and 0.05% screening risk. Risk flags and overall halts are explicitly reported.',
        '## 1. Paired cost attribution, identical full-cost executed trades',
        (output/'gross_net_same_trades.md').read_text(),
        '## 1b. Actual counterfactual reruns (entry geometry and trade sets can change)',(output/'cost_reruns.md').read_text(),
        '## 2–3. Two-hundred-seed null and zero-cost sanity', (output/'null_summary.md').read_text(),
        'Null samples one random timestamp/pair per actual London/NY session per date; same ATR/minimum stop, reward and holding rules. Directions are uniform. Empty/closed dates are not invented.',
        'A real-market random strategy need not equal zero exactly. Review sampling intervals, barrier-only win rate, gap exits, finite horizon and stop-first bias before declaring a bug.',
        (output/'real_vs_null.md').read_text(),
        '## 4. M1 bar ordering bounds',(output/'bar_ordering.md').read_text(),
        'Open-direction: O-L-H-C on up/doji bars; O-H-L-C on down bars. Heuristic only. Every policy replays the same signal tape, so later opportunity sets can differ.',
        '## 5. Modelled spread (pips)',(output/'spread_by_hour.md').read_text(),
        'No recorded spread exists in either original M1 export, so an empirical calibration is unavailable. News doubles spread when events are supplied; no supplied calendar means this unobserved widening is not present in these runs.',
        '## 6. Resolution diagnostics',(output/'resolution.md').read_text(),
        'Monthly tables/charts are under resolution/M1_zero, M1_full, M5_zero, M5_full, M15_zero, M15_full. Closed contiguous OHLC aggregation only; M1 execution retained. Indicator bar counts are unchanged; session/cooldown/holding periods stay in minutes. M5/M15 stops are 3x/6x the closed M1 ATR/five-pip rule. This is an authorized diagnostic variant, not retuning or independent evidence.',
        '## 7. Attribution criteria',
        'Use gross zero-cost reruns for gross edge, the identical-trade bridge for cost drag, null comparisons for chance performance, and ordering bounds/accounting assertions for engine bias. A nonzero random mean on actual prices alone is not proof of an implementation bug. Diagnostic evidence cannot certify historical clock/news or independent profitability.',
        'All tables and per-seed rows are retained; no seed/parameter is selected for profit.']
    (output/'diagnostic_report.md').write_text('\n\n'.join(report)+'\n',encoding='utf-8')
    log.info('DIAGNOSTICS COMPLETE: %s',output)


if __name__=='__main__': main()
