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
    assert full['trades']==sparse['trades']==compact['trades']
    assert full['final_balance']==sparse['final_balance']==compact['final_balance']
    assert not compact['open_position']


def test_random_controls_are_reproducible_direction_independent():
    data=rows(3)
    pool={('ny','2024-01-03'):[(b['ts'],'EURUSD',b['close'],.0005) for b in data]}
    first=random_tape(pool,17,2.)
    assert first==random_tape(pool,17,2.)
    assert next(iter(first.values()))['reference_distance']==.0005
    assert {next(iter(random_tape(pool,i,2.).values()))['sign'] for i in range(20)}=={-1,1}


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
