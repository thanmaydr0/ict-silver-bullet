"""Tiny existing candle fixtures test mechanics only, never strategy returns."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pytest
from backtest.scalp_policy import Costs, Limits, aware, fx_day
from backtest.scalp_signals import Detector, Specification, FAMILIES
from backtest.scalp_engine import run
from backtest.scalp_analysis import monthly, bootstrap, kelly_cap
from tests.backtest.test_replay import candles


def rows(n=3):
    source = candles(n)
    start = datetime(2024,1,3,13,0,tzinfo=timezone.utc)
    return [dict(b,ts=start+timedelta(minutes=i),open=1.1,high=1.1001,
                 low=1.0999,close=1.1) for i,b in enumerate(source)]


def one_signal(bar):
    if bar['ts'].minute == 0:
        return dict(sign=1,stop=1.099,target=1.102,
                    decision_at=bar['ts']+timedelta(minutes=1),regime='range')


def test_clock_dst_and_naive_rejection():
    assert fx_day(aware('2024-03-08T21:59:00Z')).isoformat() == '2024-03-07'
    assert fx_day(aware('2024-03-11T21:00:00Z')).isoformat() == '2024-03-11'
    with pytest.raises(ValueError): aware(datetime(2024,1,1))


def test_soft_daily_latches_and_resets_at_ny_rollover():
    s = Limits()
    s.mark(10000,aware('2024-01-03T13:00:00Z'))
    s.mark(9790,aware('2024-01-03T14:00:00Z'))
    assert s.daily_halt and s.budget(.001,10000) == 0
    s.balance = 9790
    s.mark(9790,aware('2024-01-03T22:00:00Z'))
    assert not s.daily_halt and s.day_balance == 9790


def test_floating_high_water_and_hard_breach_not_clipped():
    s = Limits()
    s.mark(10500,aware('2024-01-03T13:00:00Z'))
    flags=s.mark(9700,aware('2024-01-03T13:01:00Z'))
    assert flags['breach_overall'] and flags['overall_dd'] > .07
    assert s.overall_halt


def test_modelled_spreads_and_stress_all_cost_components():
    c=Costs()
    asia=aware('2024-01-03T06:00:00Z')
    liquid=aware('2024-01-03T14:00:00Z')
    rollover=aware('2024-01-03T22:00:00Z')
    assert c.spread('EURUSD',asia)>c.spread('EURUSD',liquid)
    assert c.spread('GBPUSD',rollover)>c.spread('GBPUSD',asia)
    stress=replace(c,multiplier=1.5)
    assert stress.spread('EURUSD',liquid,recorded=.8)==pytest.approx(1.2)
    assert stress.commission==pytest.approx(c.commission*1.5)
    assert stress.slip(True)==pytest.approx(c.slip(True)*1.5)


def test_next_open_fill_stop_first_and_cost_identity():
    data=rows()
    data[0]['low']=1.09  # A signal cannot fill on its own decision bar.
    data[1].update(low=1.098,high=1.103)
    r=run(data,Specification('compression'),candidate=one_signal,risk=.001)
    t=r['trades'][0]
    assert t['opened_at']==data[1]['ts'].isoformat()
    assert t['reason']=='stop'
    assert t['net']==pytest.approx(t['gross']-t['costs'])
    assert r['final_balance']==pytest.approx(10000+t['net'])
    assert sum(x['amount'] for x in r['costs_ledger'])==pytest.approx(t['costs'])
    assert t['path_r'][-1]==pytest.approx(t['r'])


def test_gap_cancel_and_input_integrity():
    data=rows(2)
    data[1]['ts']+=timedelta(minutes=1)
    r=run(data,Specification('compression'),candidate=one_signal)
    assert not r['trades'] and r['reasons']['missing_next_bar']==1
    with pytest.raises(ValueError):
        run([data[0],data[0]],Specification('compression'))


def test_floating_breaker_closes_before_bar_recovery():
    data=rows(3)
    # At maximum allowed 0.5% sizing, an extreme adverse bar can gap past limits.
    data[2].update(open=.9,low=.8,high=1.1001,close=1.1)
    r=run(data,Specification('compression'),candidate=one_signal,risk=.005)
    assert any(m['breach_daily'] for m in r['marks'])
    assert r['trades'][0]['reason']=='stop'
    assert r['final_balance'] < 10000


@pytest.mark.parametrize('family',FAMILIES)
def test_indicator_prefix_invariance_with_adversarial_future(family):
    data=rows(180)
    for i,b in enumerate(data):
        b['ts']-=timedelta(hours=2)
        b['close']=1.1+(i%2)*.00001
        if i<100 and family=='compression':
            b.update(high=1.102,low=1.098)
    data[135].update(open=1.1,high=1.103,low=1.0999,close=1.102)
    if family=='failed_breakout':
        data[135].update(open=1.1002,high=1.103,low=1.0999,close=1.10005)
    elif family=='mean_reversion':
        data[135].update(open=1.0994,high=1.1,low=1.0993,close=1.0995)
    prefix=data[:140]
    mutated=prefix+[dict(b,open=100.,high=101.,low=99.,close=100.) for b in data[140:]]
    def outputs(stream):
        model=Detector(Specification(family))
        return [model.update(b) for b in stream]
    expected=outputs(prefix)
    assert any(expected), 'test must exercise an actual signal, not only empty warmup'
    assert outputs(data)[:140]==expected==outputs(mutated)[:140]


def test_two_symbols_cannot_reuse_slot_freed_by_future_intrabar_exit():
    data=rows(3)
    data[1].update(low=1.098,high=1.103)
    stream=sorted(data+[dict(b,pair='GBPUSD') for b in data],key=lambda x:(x['ts'],x['pair']))
    r=run(stream,Specification('compression'),candidate=one_signal)
    assert len(r['trades'])==1


def test_months_include_zero_trade_month_and_stress_label():
    data=rows(1)+[dict(rows(1)[0],ts=aware('2024-02-01T13:00:00Z'))]
    r=run(data,Specification('compression'))
    table=monthly(r)
    assert len(table)==2 and all(x['trades']==0 for x in table)
    assert all(x['hit_10pct'] is False for x in table)
    assert 'MODELLED' in table[0]['spread_source']


def test_bootstrap_requires_floating_path_and_does_not_stop_on_target():
    start,end='2024-01-01T00:00:00Z','2024-01-30T00:00:00Z'
    with pytest.raises(ValueError):
        bootstrap([dict(closed_at=start,r=-1)],risk=.001,first_day=start,last_day=end)
    t=dict(closed_at='2024-01-01T23:00:00Z',r=-1,path_r=[200,-1])
    r=bootstrap([t],risk=.001,first_day=start,last_day=end,block_days=30,simulations=2)
    assert r['p_10pct']==0 and r['p_overall_breach']==1
    assert r['p_union_breach']==1


def test_kelly_cannot_authorize_risk_from_tiny_sample():
    assert kelly_cap([])['cap']==0


def test_historical_news_gate_blocks_and_flattens():
    data=rows(3)
    blocked=run(data,Specification('compression'),candidate=one_signal,
        news_events=[dict(currency='USD',impact='high',scheduled_at='2024-01-03T13:15:00Z')])
    assert not blocked['trades']
    exited=run(data,Specification('compression'),candidate=one_signal,
        news_events=[dict(currency='USD',impact='high',scheduled_at='2024-01-03T13:17:00Z')])
    assert exited['trades'][0]['reason']=='session_news_time_exit'
    assert exited['trades'][0]['closed_at']==data[2]['ts'].isoformat()


def test_rolling_windows_have_six_full_tune_months_and_no_holdout():
    from backtest.scalp_walkforward import folds
    first=aware('2023-10-18T05:49:00Z')
    holdout=aware('2026-07-01T00:00:00Z')
    windows=folds(first,holdout)
    assert windows[0][0]==aware('2023-11-01T00:00:00Z')
    assert windows[0][1]==aware('2024-05-01T00:00:00Z')
    assert windows[-1][2]==holdout
    assert all(a<b<c<=holdout for a,b,c in windows)


def test_source_clock_scenarios_disagree_in_dst_mismatch_week():
    import pandas as pd
    from backtest.scalp_study import clock_scenario
    ts=pd.Series(pd.to_datetime(['2024-03-18T09:00:00Z','2024-07-01T09:00:00Z']))
    eu=clock_scenario(ts,'broker_eu')
    us=clock_scenario(ts,'broker_us')
    assert eu.iloc[0].hour==7 and us.iloc[0].hour==6
    assert eu.iloc[1]==us.iloc[1]
    assert clock_scenario(ts,'label_utc').equals(ts)
    with pytest.raises(ValueError): clock_scenario(ts.dt.tz_localize(None),'label_utc')


def test_source_fingerprint_is_a_precondition(tmp_path):
    from backtest.scalp_study import read_data
    with pytest.raises(FileNotFoundError): read_data(tmp_path,'label_utc')
    (tmp_path/'EURUSD_M1.parquet').write_bytes(b'not a price file')
    with pytest.raises(ValueError,match='fingerprint'): read_data(tmp_path,'label_utc')


def test_monthly_chart_and_csv_are_written_from_account_marks(tmp_path):
    pytest.importorskip('matplotlib')
    from backtest.scalp_study import write_run
    data=rows(3); data[1].update(low=1.098,high=1.103)
    report=run(data,Specification('compression'),candidate=one_signal)
    table=write_run(tmp_path,report,{'2024-01':'UNIT_TEST_ONLY'})
    assert table[0]['tag']=='UNIT_TEST_ONLY'
    assert (tmp_path/'2024-01.png').read_bytes().startswith(b'\x89PNG')
    assert (tmp_path/'summary.csv').is_file()
    assert (tmp_path/'trades.jsonl.gz').is_file()


def test_sharp_parameter_peak_is_rejected_without_retuning():
    from backtest.scalp_analysis import robustness
    cells=[dict(stop_atr=s,reward=r,average_r=.6,stress=dict(average_r=.1))
           for s in (1.25,1.5,1.75) for r in (1.25,1.5,1.75)]
    cells[4]['average_r']=1.
    assert robustness(cells,1.5,1.5)['passed']
    for i,c in enumerate(cells):
        if i!=4: c['average_r']=.01
    assert not robustness(cells,1.5,1.5)['passed']
