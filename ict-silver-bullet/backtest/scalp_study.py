"""Offline study IO, preflight and report writer. No network, database or MT5.

Run: python -m backtest.scalp_study --data-dir <existing-export> --clock label_utc
Clock scenarios are conditional; this command does not certify source time.
"""
import argparse
import csv
from dataclasses import asdict
from datetime import timedelta
import gzip
import hashlib
import heapq
import json
from pathlib import Path
from uuid import uuid4
from backtest.scalp_policy import Costs, aware
from backtest.scalp_signals import Specification
from backtest.scalp_engine import run
from backtest.scalp_analysis import monthly, summary, metrics
from brain.logging_setup import setup_logging

SOURCE_SHA256 = {
    'EURUSD_M1.parquet':'0d5c5d2bd2cb8037a3bd5a9d2437ef234629736a50b8747017d821ffb26fcc3a',
    'GBPUSD_M1.parquet':'656f4acb24ccf08fb2a538ab0bfdc88663b90230982b32df67be5f2decee84ac',
}
RETROSPECTIVE_HOLDOUT = '2026-07-01T00:00:00Z'


def clock_scenario(ts,clock):
    """Explicit sensitivity transforms, not inferred historical truth."""
    import pandas as pd
    if ts.dt.tz is None:
        raise ValueError('naive timestamps: explicit provenance needed')
    if clock=='broker_eu':
        return ts.dt.tz_localize(None).dt.tz_localize('Europe/Helsinki',ambiguous='raise',nonexistent='raise').dt.tz_convert('UTC')
    if clock=='broker_us':
        local=ts.dt.tz_localize(None)-pd.Timedelta(hours=7)
        return local.dt.tz_localize('America/New_York',ambiguous='raise',nonexistent='raise').dt.tz_convert('UTC')
    if clock=='label_utc':
        return ts.dt.tz_convert('UTC')
    raise ValueError('unknown clock scenario')


def trial(folder,spec,costs,clock,start,end,stage,ledger,*,risk=.0005,extra=None):
    """Write the trial BEFORE touching prices; append completed status separately.

    Prior-trial history is unknown, so this ledger never claims a lifetime count.
    """
    path=Path(ledger); path.parent.mkdir(parents=True,exist_ok=True)
    fields=['trial_id','stage','strategy','parameters','data_fingerprint','tune_start','tune_end',
            'validate_start','validate_end','cost_multiplier','status','result_path']
    entry=dict(trial_id=str(uuid4()),stage=stage,strategy=spec.family,
        parameters=json.dumps(dict(spec=asdict(spec),clock=clock,risk=risk,
                                   costs=asdict(costs),**(extra or {})),sort_keys=True),
        data_fingerprint=json.dumps(SOURCE_SHA256,sort_keys=True),
        tune_start=start if stage=='TUNE' else '',tune_end=end if stage=='TUNE' else '',
        validate_start=start if stage!='TUNE' else '',validate_end=end if stage!='TUNE' else '',
        cost_multiplier=costs.multiplier,status='STARTED',result_path=str(folder))
    with path.open('a',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields)
        if path.stat().st_size==0: w.writeheader()
        w.writerow(entry)
    return entry


def finish_trial(entry,ledger,status):
    path=Path(ledger)
    with path.open(newline='',encoding='utf-8') as f:
        reader=csv.DictReader(f); fields=reader.fieldnames; rows=list(reader)
    for row in rows:
        if row['trial_id']==entry['trial_id']: row['status']=status
    temp=path.with_suffix('.csv.tmp')
    with temp.open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    temp.replace(path)


def read_data(folder, clock):
    import pandas as pd
    import numpy as np
    if clock not in ('label_utc','broker_eu','broker_us'):
        raise ValueError('explicit source-clock scenario required')
    frames, audit = {}, []
    for name, expected in SOURCE_SHA256.items():
        path=Path(folder)/name
        if not path.is_file():
            raise FileNotFoundError(f'Existing source file unavailable locally: {path}')
        sha=hashlib.sha256(path.read_bytes()).hexdigest()
        if sha != expected:
            raise ValueError(f'source fingerprint changed: {name}; audit it before running')
        frame=pd.read_parquet(path)
        if not set(('pair','ts','open','high','low','close')) <= set(frame):
            raise ValueError('source columns missing')
        ts=pd.to_datetime(frame['ts'])
        if ts.dt.tz is None:
            raise ValueError('naive timestamps: explicit provenance needed')
        if ts.isna().any() or ts.duplicated().any() or not ts.is_monotonic_increasing:
            raise ValueError('missing, duplicate or out-of-order source timestamps')
        if ((ts.dt.second != 0) | (ts.dt.microsecond != 0) | (ts.dt.nanosecond != 0)).any():
            raise ValueError('source is not on the M1 grid')
        symbol=name.split('_')[0]
        if set(frame['pair']) != {symbol}:
            raise ValueError('symbol contents do not match the source file')
        if 'timeframe' in frame and set(frame['timeframe']) != {'M1'}:
            raise ValueError('M1 bars required')
        if 'spread' in frame and 'spread_pips' not in frame:
            raise ValueError('raw spread units unknown; supply an audited pips conversion')
        prices=frame[['open','high','low','close']].to_numpy(dtype=float)
        if not np.isfinite(prices).all() or (prices<=0).any():
            raise ValueError('source contains missing/nonpositive/nonfinite OHLC')
        op,hi,lo,cl=prices.T
        if not ((lo<=np.minimum(op,cl)) & (np.maximum(op,cl)<=hi)).all():
            raise ValueError('source OHLC geometry invalid')
        gaps=ts.diff().dt.total_seconds()/60
        # No price values are shifted, filled, generated or resampled.
        ts=clock_scenario(ts,clock)
        frame=frame.copy()
        frame['ts']=ts
        audit.append(dict(file=name,sha256=sha,rows=len(frame),columns=list(frame),
            first=ts.min().isoformat(),last=ts.max().isoformat(),clock_scenario=clock,
            clock_verified=False,duplicates=0,invalid_ohlc=0,
            gaps_gt_one_minute=int((gaps>1).sum()),largest_gap_minutes=float(gaps.max()),
            absent_calendar_minutes=int((gaps[gaps>1]-1).sum()),
            missing_tradable_minutes=None,market_calendar_verified=False,
            spread_source='recorded pips' if 'spread_pips' in frame else 'MODELLED'))
        frames[symbol]=frame
    return frames,audit


def stream(frames, start=None, end=None):
    """Heap merge immutable views; no millions-of-dictionaries history copy."""
    def one(frame):
        if start:
            frame=frame[frame.ts >= aware(start)-timedelta(days=10)]
        if end:
            frame=frame[frame.ts < aware(end)]
        cols=list(frame)
        for values in frame.itertuples(index=False,name=None):
            row=dict(zip(cols,values))
            row['ts']=row['ts'].to_pydatetime()
            yield row
    return heapq.merge(*(one(frame) for frame in frames.values()),key=lambda r:(r['ts'],r['pair']))


def write_json(path,value):
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2,default=str,allow_nan=False),encoding='utf-8')


def write_run(folder,report,tags=None):
    """One table, report and actual equity chart per observed calendar month."""
    folder=Path(folder)
    folder.mkdir(parents=True,exist_ok=True)
    table=monthly(report,tags)
    with (folder/'summary.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=list(table[0]) if table else ['month'])
        w.writeheader(); w.writerows(table)
    fields=['month','tag','trades','average_r','profit_factor','net_return_pct',
            'worst_daily_dd_pct','max_overall_dd_pct','hit_10pct','breach_daily','breach_overall']
    text='Spreads: **MODELLED unless explicitly recorded in pips**. Retrospective evidence only.\n\n'
    text+='| '+' | '.join(fields)+' |\n| '+' | '.join(['---']*len(fields))+' |\n'
    text+='\n'.join('| '+' | '.join(str(r[k]) for k in fields)+' |' for r in table)
    (folder/'summary.md').write_text(text+'\n',encoding='utf-8')
    write_json(folder/'aggregate.json',dict(metrics=metrics(report['trades']),summary=summary(table),
        open_position=report['open_position'],news_complete=report['news_complete'],
        live_eligible=False,latency_model=report['latency_model'],intrabar_model=report['intrabar_model']))
    with gzip.open(folder/'trades.jsonl.gz','wt',encoding='utf-8') as f:
        for t in report['trades']: f.write(json.dumps(t,allow_nan=False)+'\n')
    with gzip.open(folder/'equity.jsonl.gz','wt',encoding='utf-8') as f:
        for m in report['marks']: f.write(json.dumps(m,allow_nan=False)+'\n')
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import pyplot as plt
    for row in table:
        month=row['month']
        points=[m for m in report['marks'] if m['ts'].startswith(month)]
        write_json(folder/f'{month}.json',row)
        fig,ax=plt.subplots(figsize=(9,3))
        ax.plot([aware(m['ts']) for m in points],[m['equity'] for m in points],lw=.8)
        ax.set_title(f'{month} — {row["tag"]} — spread MODELLED unless recorded')
        ax.set_ylabel('Floating liquidation equity (USD)')
        ax.grid(alpha=.2); fig.autofmt_xdate(); fig.tight_layout()
        fig.savefig(folder/f'{month}.png',dpi=120); plt.close(fig)
    if report['marks']:
        fig,ax=plt.subplots(figsize=(12,4))
        ax.plot([aware(m['ts']) for m in report['marks']],[m['equity'] for m in report['marks']],lw=.7)
        ax.set_title('Compounded retrospective equity; modelled spreads / conditional clock')
        fig.autofmt_xdate(); fig.tight_layout(); fig.savefig(folder/'equity.png',dpi=120); plt.close(fig)
    return table


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir',type=Path,required=True)
    parser.add_argument('--clock',choices=('label_utc','broker_eu','broker_us'),required=True)
    parser.add_argument('--family',choices=('opening_range','failed_breakout','mean_reversion','compression'),required=True)
    parser.add_argument('--reward',type=float,default=1.5)
    parser.add_argument('--cost-multiplier',type=float,default=1.)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--news',type=Path)
    parser.add_argument('--start')
    parser.add_argument('--end',default=RETROSPECTIVE_HOLDOUT)
    args=parser.parse_args()
    logger=setup_logging('scalp-study')
    if aware(args.end) > aware(RETROSPECTIVE_HOLDOUT):
        parser.error('recent holdout requires a separately frozen final configuration; this screening CLI cannot open it')
    args.output.mkdir(parents=True,exist_ok=True)
    spec=Specification(args.family,reward=args.reward)
    costs=Costs(multiplier=args.cost_multiplier)
    ledger=Path(__file__).resolve().parents[1]/'reports'/'trials_ledger.csv'
    entry=trial(args.output,spec,costs,args.clock,args.start,args.end,'DEVELOPMENT',ledger)
    write_json(args.output/'specification.json',dict(strategy=asdict(spec),costs=asdict(costs),
        clock_scenario=args.clock,clock_verified=False,start=args.start,end=args.end,
        evidence='previously examined retrospective development data',status='started'))
    try:
        frames,audit=read_data(args.data_dir,args.clock)
    except Exception:
        finish_trial(entry,ledger,'BLOCKED_PREFLIGHT')
        raise
    write_json(args.output/'data_audit.json',audit)
    events=json.loads(args.news.read_text(encoding='utf-8')) if args.news else []
    report=run(stream(frames,args.start,args.end),spec,costs=costs,start=args.start,end=args.end,
               news_events=events,news_complete=False)
    write_run(args.output,report)
    finish_trial(entry,ledger,'COMPLETE')
    logger.info('Conditional retrospective run complete; no live eligibility; %s',metrics(report['trades']))


if __name__=='__main__':
    main()
