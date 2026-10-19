"""Partition the predeclared null seeds without changing the experiment.

Workers own disjoint seed directories and separate ledgers. The report builder
merges ledgers only after both workers finish. No shared mutable result table.
"""
import argparse
from datetime import timedelta
import json
from pathlib import Path
from backtest.scalp_diagnostics import (DiagnosticCosts,random_pool,random_tape,
    sparse_index,event_cache,event_stream,describe,table)
from backtest.scalp_signals import Specification
from backtest.scalp_study import read_data,write_json,trial,finish_trial
from backtest.scalp_policy import aware
from backtest.scalp_engine import run
from brain.logging_setup import setup_logging


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--worker',type=int,required=True,choices=(0,1))
    args=p.parse_args();root=Path(__file__).resolve().parents[1];output=root/'reports/diagnostics'
    log=setup_logging(f'scalp-null-{args.worker}')
    protocol=json.loads((output/'protocol.json').read_text())
    if protocol['seeds']!=200 or protocol['risk']!=.000001 or protocol['initial']!=10000000.:
        raise ValueError('unexpected null protocol')
    frames,audit=read_data(root/'backtest/data/validation_20261018','label_utc')
    first=min(aware(a['first']) for a in audit);end=max(aware(a['last']) for a in audit)+timedelta(minutes=1)
    pool=random_pool(frames);cached=event_cache(frames,sparse_index(frames))
    ledger=output/f'null_worker_{args.worker}_trials.csv';rows=[]
    controls=[(f'{mode}_R{rr:g}',rr,mode) for rr in (1.5,2.) for mode in ('zero','full')]+[('zero_symmetric',1.,'zero')]
    for seed in range(args.worker,200,2):
        for label,reward,mode in controls:
            folder=output/f'null/seed_{seed:03d}/{label}';saved=folder/'diagnostic.json'
            if saved.exists():
                stat=json.loads(saved.read_text())
            else:
                spec=Specification('compression',reward=reward);settings=DiagnosticCosts(mode=mode)
                entry=trial(folder,spec,settings,'label_utc',first.isoformat(),end.isoformat(),
                    'DIAGNOSTIC',ledger,risk=.000001,
                    extra=dict(seed=seed,worker=args.worker,initial=10000000.,name=f'null/seed_{seed:03d}/{label}',
                               ambiguity='stop_first',archive='summary only; deterministic seed/source allow replay'))
                log.info('START seed=%s %s',seed,label)
                try:
                    signals=random_tape(pool,seed,reward);activity={}
                    report=run(event_stream(cached,signals,activity),spec,costs=settings,
                        risk=.000001,initial=10000000.,start=first,end=end,activity=activity,
                        retain_marks=False,retain_paths=False,candidate=lambda r:signals.get((r['ts'],r['pair'])))
                    stat=describe(report)
                    if stat['open_position'] or stat['accounting_max_error']>1e-6:
                        raise ValueError('null accounting or boundary check failed')
                    saved.parent.mkdir(parents=True,exist_ok=True)
                    write_json(saved.with_suffix('.tmp'),stat);saved.with_suffix('.tmp').replace(saved)
                    finish_trial(entry,ledger,'COMPLETE')
                except Exception:
                    finish_trial(entry,ledger,'FAILED');raise
            rows.append(dict(seed=seed,control=label,**stat))
        table(output/f'null_worker_{args.worker}',rows)
        log.info('COMPLETE seed=%s (%s/100 worker seeds)',seed,len(rows)//5)
    write_json(output/f'null_worker_{args.worker}_complete.json',dict(worker=args.worker,seeds=100,controls=5,rows=len(rows)))
    log.info('NULL WORKER COMPLETE')


if __name__=='__main__':main()
