"""One frozen-logic clock ablation, chosen from closures, never from profits.

Run after the main diagnostic worker exits, to keep ledger writes sequential.
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
    if not (output/'complete.json').exists():
        raise ValueError('main diagnostic must finish before this clock ablation')
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
    write_json(folder/'protocol.json',dict(reason='156/156 weekly closes per pair align near 17:00 NY under EU-server interpretation; 0/156 under UTC labels',
        inferred=True,broker_certified=False,spec=spec.__dict__,clock='broker_eu',risk=.000001,initial=10000000.))
    write_json(folder/'data_audit.json',audit)
    first=min(aware(a['first']) for a in audit);end=max(aware(a['last']) for a in audit)+timedelta(minutes=1)
    signals=tape(frames,spec);cached=event_cache(frames,sparse_index(frames))
    for mode in ('zero','full'):
        path=folder/mode;settings=DiagnosticCosts(mode=mode)
        if (path/'diagnostic.json').exists():
            d=json.loads((path/'diagnostic.json').read_text())
        else:
            entry=trial(path,spec,settings,'broker_eu',first.isoformat(),end.isoformat(),
                'DIAGNOSTIC_CLOCK',root/'reports/trials_ledger.csv',risk=.000001,
                extra=dict(initial=10000000.,reason='closure-aligned inference, no profit selection'))
            log.info('START clock comparison %s',mode)
            try:
                activity={}
                rep=run(event_stream(cached,signals,activity),spec,costs=settings,risk=.000001,
                    initial=10000000.,start=first,end=end,activity=activity,
                    candidate=lambda r:signals.get((r['ts'],r['pair'])))
                d=describe(rep);write_run(path,rep,{m:'DIAGNOSTIC_INFERRED_CLOCK' for m in rep['months']})
                write_json(path/'diagnostic.json',d);finish_trial(entry,root/'reports/trials_ledger.csv','COMPLETE')
            except Exception:
                finish_trial(entry,root/'reports/trials_ledger.csv','FAILED');raise
        rows.append(dict(clock='broker_eu',family=spec.family,mode=mode,**d))
    table(output/'clock_ablation',rows);log.info('CLOCK ABLATION COMPLETE')


if __name__=='__main__':main()
