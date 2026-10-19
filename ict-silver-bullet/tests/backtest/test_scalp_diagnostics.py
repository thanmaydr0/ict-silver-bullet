"""Mechanical diagnostic tests; artificial fixtures never count as price evidence."""
from datetime import timedelta
from dataclasses import replace
import pytest
from tests.backtest.test_scalp_validation import rows,one_signal
from backtest.scalp_engine import run
from backtest.scalp_signals import Specification,Detector
from backtest.scalp_diagnostics import DiagnosticCosts,describe,sparse_frames,random_tape,tape


def test_zero_and_cost_stage_switches_are_exact():
    from backtest.scalp_policy import aware
    now=aware('2024-01-03T13:00:00Z')
    zero=DiagnosticCosts(mode='zero')
    assert zero.spread('EURUSD',now,9)==zero.slip(True)==zero.commission==0
    spread=DiagnosticCosts(mode='spread')
    assert spread.spread('EURUSD',now)>0 and spread.slip()==spread.commission==0
    fees=DiagnosticCosts(mode='spread_commission')
    assert fees.commission==7 and fees.slip(True)==0
    assert DiagnosticCosts().slip(True)==.3


@pytest.mark.parametrize('sign',[-1,1])
def test_bar_ordering_has_known_payouts_and_cash_identity(sign):
    data=rows(3)
    data[1].update(open=1.1,high=1.103,low=1.097,close=1.1001)
    def signal(row):
        if row['ts']==data[0]['ts']:
            return dict(sign=sign,stop=1.1-sign*.001,target=1.1+sign*.001,
                reference_distance=.001,decision_at=row['ts']+timedelta(minutes=1),regime='unit')
    spec=Specification('compression',reward=1.)
    stop=describe(run(data,spec,costs=DiagnosticCosts(mode='zero'),candidate=signal))
    target=describe(run(data,spec,costs=DiagnosticCosts(mode='zero'),candidate=signal,ambiguity='target_first'))
    heuristic=describe(run(data,spec,costs=DiagnosticCosts(mode='zero'),candidate=signal,ambiguity='open_direction'))
    assert stop['net_r']==pytest.approx(-1)
    assert target['net_r']==pytest.approx(1)
    assert heuristic['net_r']==pytest.approx(-1 if sign>0 else 1)
    assert stop['ambiguous_trades']==1 and stop['ambiguous_share_pct']==100
    assert target['accounting_max_error']<1e-8


def test_opening_gap_stop_cannot_be_overridden_by_target_first():
    data=rows(4)
    data[2].update(open=1.098,low=1.097,high=1.103,close=1.1)
    spec=Specification('compression')
    r=run(data,spec,candidate=one_signal,costs=DiagnosticCosts(mode='zero'),ambiguity='target_first')
    assert r['trades'][0]['reason']=='stop' and r['trades'][0]['r']<-1


def test_known_open_target_precedes_a_later_stop_even_under_stop_first():
    data=rows(4)
    data[2].update(open=1.103,high=1.104,low=1.098,close=1.1)
    spec=Specification('compression')
    old=run(data,spec,candidate=one_signal,costs=DiagnosticCosts(mode='zero'),known_open_priority=False)
    fixed=run(data,spec,candidate=one_signal,costs=DiagnosticCosts(mode='zero'))
    assert old['trades'][0]['reason']=='stop'
    assert fixed['trades'][0]['reason']=='target_at_open'
    assert fixed['trades'][0]['gross']>0 and old['trades'][0]['gross']<0
    assert fixed['reasons']['known_open_target_priority']==1


def test_sparse_null_stream_matches_full_existing_engine(tmp_path):
    import pandas as pd
    data=rows(200)
    data[50].update(high=1.103,low=1.098)
    source={'EURUSD':pd.DataFrame(data)}
    signals={(data[0]['ts'],'EURUSD'):dict(one_signal(data[0]),reference_distance=.001)}
    spec=Specification('compression')
    candidate=lambda r:signals.get((r['ts'],r['pair']))
    from backtest.scalp_study import stream
    full=run(stream(source),spec,candidate=candidate)
    sparse=run(stream(sparse_frames(source,signals)),spec,candidate=candidate)
    compact=run(stream(sparse_frames(source,signals)),spec,candidate=candidate,retain_marks=False)
    from backtest.scalp_diagnostics import sparse_index,event_cache,event_stream
    activity={}
    event=run(event_stream(event_cache(source,sparse_index(source)),signals,activity),
              spec,candidate=candidate,activity=activity)
    assert full['trades']==sparse['trades']==compact['trades']==event['trades']
    assert full['final_balance']==sparse['final_balance']==compact['final_balance']
    assert not compact['open_position']
    import json
    json.dumps(event['trades'],allow_nan=False)


def test_random_controls_are_reproducible_direction_independent():
    data=rows(3)
    pool={('ny','2024-01-03'):[(b['ts'],'EURUSD',b['close'],.0005) for b in data]}
    first=random_tape(pool,17,2.)
    assert first==random_tape(pool,17,2.)
    assert next(iter(first.values()))['reference_distance']==.0005
    assert {next(iter(random_tape(pool,i,2.).values()))['sign'] for i in range(20)}=={-1,1}


def test_event_seek_matches_full_two_symbol_replay_with_gaps_and_breakers():
    import pandas as pd
    from backtest.scalp_diagnostics import sparse_index,event_cache,event_stream
    from backtest.scalp_study import stream
    data=rows(400)
    for i,row in enumerate(data):
        if i%20==2: row.update(open=1.095,high=1.103,low=1.094)
    frames={'EURUSD':pd.DataFrame(data),
            'GBPUSD':pd.DataFrame([dict(b,pair='GBPUSD') for i,b in enumerate(data) if i not in (42,43)])}
    signals={}
    for symbol,f in frames.items():
        for row in stream({symbol:f}):
            if row['ts'].minute%20==0:
                signals[(row['ts'],symbol)]=dict(sign=1,stop=1.099,target=1.102,
                    reference_distance=.001,decision_at=row['ts']+timedelta(minutes=1),regime='unit')
    callback=lambda r:signals.get((r['ts'],r['pair']))
    spec=Specification('compression');activity={}
    full=run(stream(frames),spec,candidate=callback,risk=.005)
    fast=run(event_stream(event_cache(frames,sparse_index(frames)),signals,activity),
             spec,candidate=callback,risk=.005,activity=activity)
    assert full['trades']==fast['trades'] and full['final_balance']==fast['final_balance']
    assert any(m['soft_daily'] for m in full['marks'])
    activity={}
    lean=run(event_stream(event_cache(frames,sparse_index(frames)),signals,activity),
             spec,candidate=callback,risk=.005,activity=activity,retain_marks=False,retain_paths=False)
    without_paths=lambda r:[{k:v for k,v in t.items() if k!='path_r'} for t in r['trades']]
    assert without_paths(full)==without_paths(lean)
    assert full['risk_flags_seen']==lean['risk_flags_seen']
    assert full['overall_halt']==lean['overall_halt']
    assert lean['marks']==[] and all(t['path_r']==[] for t in lean['trades'])


def test_m5_tape_is_causal_and_has_no_incomplete_aggregate_signal():
    import pandas as pd
    data=rows(1400)
    # Compare prefixes at multiple aggregate boundaries; no future row may alter
    # an emitted candidate or cause an incomplete aggregate to become available.
    spec=Specification('compression')
    full=tape({'EURUSD':pd.DataFrame(data)},spec,5,3.)
    prefix=tape({'EURUSD':pd.DataFrame(data[:1277])},spec,5,3.)
    assert prefix=={k:v for k,v in full.items() if k[0]<=data[1276]['ts']}
    assert all((ts.minute+1)%5==0 for ts,_ in full)


def test_unchanged_m1_detector_default_equals_explicit_resolution():
    a=Detector(Specification('compression'));b=Detector(Specification('compression'),1)
    for row in rows(150): assert a.update(row)==b.update(row)
