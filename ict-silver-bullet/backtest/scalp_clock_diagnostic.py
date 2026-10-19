"""One frozen-logic clock ablation, chosen from closures, never from profits.

Uses a separate ledger/output, so it can run independently of the main worker.
"""
from datetime import timedelta
import json
from pathlib import Path
from backtest.scalp_diagnostics import DiagnosticCosts,tape,sparse_index,event_cache,event_stream,describe,table
from backtest.scalp_study import read_data,write_json,write_run,trial,finish_trial
from backtest.scalp_signals import Specification
from backtest.scalp_engine import run
from backtest.scalp_policy import aware
from brain.logging_setup import setup_logging


def main():
    root=Path(__file__).resolve().parents[1];output=root/'reports/diagnostics'
    log=setup_logging('scalp-clock-diagnostic')
    spec=Specification(**json.loads((root/'reports/frozen_holdout_receipt.json').read_text())['strategy'])
    rows=[]
    for mode in ('zero','full'):
        d=json.loads((output/spec.family/mode/'diagnostic.json').read_text())
        rows.append(dict(clock='label_utc',family=spec.family,mode=mode,**d))
    # This transform is selected only by weekly-closure alignment; not certified
    # broker provenance. Existing prices, rules and risk allocation stay fixed.
    frames,audit=read_data(root/'backtest/data/validation_20261018','broker_eu')
    folder=output/'clock_ablation';folder.mkdir(parents=True,exist_ok=True)
    ledger=folder/'trials_ledger.csv'
    write_json(folder/'protocol.json',dict(reason='156/156 weekly closes per pair align near 17:00 NY under EU-server interpretation; 0/156 under UTC labels',
        inferred=True,broker_certified=False,spec=spec.__dict__,clock='broker_eu',risk=.000001,initial=10000000.))
    write_json(folder/'data_audit.json',audit)
    first=min(aware(a['first']) for a in audit);end=max(aware(a['last']) for a in audit)+timedelta(minutes=1)
    signals=tape(frames,spec) if any(not (folder/m/'diagnostic.json').exists() for m in ('zero','full')) else {}
    cached=event_cache(frames,sparse_index(frames))
    for mode in ('zero','full'):
        path=folder/mode;settings=DiagnosticCosts(mode=mode)
        if (path/'diagnostic.json').exists():
            d=json.loads((path/'diagnostic.json').read_text())
        else:
            entry=trial(path,spec,settings,'broker_eu',first.isoformat(),end.isoformat(),
                'DIAGNOSTIC_CLOCK',ledger,risk=.000001,
                extra=dict(initial=10000000.,reason='closure-aligned inference, no profit selection'))
            log.info('START clock comparison %s',mode)
            try:
                activity={}
                rep=run(event_stream(cached,signals,activity),spec,costs=settings,risk=.000001,
                    initial=10000000.,start=first,end=end,activity=activity,
                    candidate=lambda r:signals.get((r['ts'],r['pair'])),known_open_priority=False)
                d=describe(rep);write_run(path,rep,{m:'DIAGNOSTIC_INFERRED_CLOCK' for m in rep['months']})
                write_json(path/'diagnostic.json',d);finish_trial(entry,ledger,'COMPLETE')
            except Exception:
                finish_trial(entry,ledger,'FAILED');raise
        rows.append(dict(clock='broker_eu',family=spec.family,mode=mode,**d))
    table(output/'clock_ablation',rows)
    write_json(folder/'resolution_protocol.json',dict(reason='same user-requested M5/M15 checks under closure-inferred clock; no parameter selection',
        clock='broker_eu',stop_scales={'M5':3,'M15':6},risk=.0005,initial=10000.,fills='M1'))
    resolution=[]
    for minutes,scale in ((5,3.),(15,6.)):
        signals=tape(frames,spec,minutes,scale)
        for mode in ('zero','full'):
            original=json.loads((output/f'resolution/M{minutes}_{mode}/diagnostic.json').read_text())
            resolution.append(dict(clock='label_utc',minutes=minutes,mode=mode,**original))
            path=folder/f'M{minutes}_{mode}';settings=DiagnosticCosts(mode=mode)
            if (path/'diagnostic.json').exists():
                d=json.loads((path/'diagnostic.json').read_text())
            else:
                entry=trial(path,spec,settings,'broker_eu',first.isoformat(),end.isoformat(),
                    'DIAGNOSTIC_CLOCK',ledger,risk=.0005,
                    extra=dict(initial=10000.,minutes=minutes,stop_scale=scale,reason='closure-selected clock; unchanged fixed rules'))
                log.info('START clock comparison M%s %s',minutes,mode)
                try:
                    activity={}
                    rep=run(event_stream(cached,signals,activity),spec,costs=settings,risk=.0005,
                        initial=10000.,start=first,end=end,activity=activity,
                        candidate=lambda r:signals.get((r['ts'],r['pair'])),known_open_priority=False)
                    d=describe(rep);write_run(path,rep,{m:'DIAGNOSTIC_INFERRED_CLOCK' for m in rep['months']})
                    write_json(path/'diagnostic.json',d);finish_trial(entry,ledger,'COMPLETE')
                except Exception:
                    finish_trial(entry,ledger,'FAILED');raise
            resolution.append(dict(clock='broker_eu',minutes=minutes,mode=mode,**d))
    table(output/'clock_resolution',resolution);log.info('CLOCK ABLATION COMPLETE')


if __name__=='__main__':main()
