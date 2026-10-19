"""Measure the known-opening-target ordering fix without changing any signals."""
import json
from datetime import timedelta
from pathlib import Path
from backtest.scalp_diagnostics import DiagnosticCosts,tape,sparse_index,event_cache,event_stream,describe,table
from backtest.scalp_study import read_data,trial,finish_trial,write_json,write_run
from backtest.scalp_signals import Specification
from backtest.scalp_policy import aware
from backtest.scalp_engine import run
from brain.logging_setup import setup_logging


def main():
    root=Path(__file__).resolve().parents[1];output=root/'reports/diagnostics'
    folder=output/'known_open_fix';folder.mkdir(parents=True,exist_ok=True)
    ledger=folder/'trials_ledger.csv';log=setup_logging('scalp-gap-diagnostic')
    frames,audit=read_data(root/'backtest/data/validation_20261018','label_utc')
    first=min(aware(a['first']) for a in audit);end=max(aware(a['last']) for a in audit)+timedelta(minutes=1)
    cache=event_cache(frames,sparse_index(frames));specs=json.loads((output/'fixed_specifications.json').read_text())
    write_json(folder/'protocol.json',dict(fix='target crossed at known bar open precedes a later stop',
        families=['mean_reversion','compression'],modes=['zero','full'],
        signals='unchanged fixed specifications',risk=.000001,initial=10000000.,clock='label_utc',
        conservative_fill='target level; no favorable opening-price improvement credited'))
    rows=[]
    for family in ('mean_reversion','compression'):
        spec=Specification(**specs[family]);log.info('CACHE %s',family);signals=tape(frames,spec)
        for mode in ('zero','full'):
            old=json.loads((output/family/mode/'diagnostic.json').read_text())
            path=folder/f'{family}_{mode}';settings=DiagnosticCosts(mode=mode)
            if (path/'diagnostic.json').exists():
                stat=json.loads((path/'diagnostic.json').read_text())
            else:
                entry=trial(path,spec,settings,'label_utc',first.isoformat(),end.isoformat(),
                    'DIAGNOSTIC_GAP_FIX',ledger,risk=.000001,
                    extra=dict(initial=10000000.,known_open_priority=True,fix='known target at open precedes later stop'))
                log.info('START %s %s',family,mode)
                try:
                    activity={}
                    report=run(event_stream(cache,signals,activity),spec,costs=settings,risk=.000001,
                        initial=10000000.,start=first,end=end,activity=activity,known_open_priority=True,
                        candidate=lambda r:signals.get((r['ts'],r['pair'])))
                    stat=describe(report)
                    write_run(path,report,{m:'DIAGNOSTIC_KNOWN_OPEN_CORRECTION' for m in report['months']})
                    write_json(path/'diagnostic.json',stat);finish_trial(entry,ledger,'COMPLETE')
                except Exception:
                    finish_trial(entry,ledger,'FAILED');raise
            rows.append(dict(family=family,mode=mode,old_trades=old['trades'],corrected_trades=stat['trades'],
                legacy_R=old['net_r'],corrected_R=stat['net_r'],delta_R=stat['net_r']-old['net_r'],
                known_open_targets=stat['known_open_target_resolutions'],accounting_max_error=stat['accounting_max_error']))
    table(output/'known_open_fix',rows);log.info('KNOWN-OPEN FIX COMPARISON COMPLETE')


if __name__=='__main__':main()
