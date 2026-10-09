from datetime import datetime, timedelta, timezone
import json

import pytest

from backtest import replay as module
from brain import app


BASE = datetime(2024, 1, 2, 15, tzinfo=timezone.utc)


def candles(count=4):
    return [dict(pair="EURUSD", timeframe="M1", ts=BASE + timedelta(minutes=i),
                 open=1.1, high=1.102, low=1.098, close=1.1) for i in range(count)]


def setup(rows, context):
    return dict(pair="EURUSD", direction="buy", entry=1.1, stop_loss=1.099,
                take_profit=1.102, htf_bias="bullish", sweep={"index": 0},
                mss={"displacement": .002}, atr=.001,
                fvg={"top": 1.101, "bottom": 1.099},
                factors=dict.fromkeys(app.confluence.FACTOR_POINTS, True))


def test_no_lookahead_and_full_audit(monkeypatch, tmp_path):
    original = app.evaluate_candles
    observed = []

    def evaluate(rows, context):
        assert all(c["ts"] + timedelta(minutes=1) <= context["now"] for c in rows)
        assert all(c["ts"] + timedelta(hours=1) <= context["now"] for c in context["htf_candles"])
        observed.append((context["now"], len(rows)))
        context = dict(context, detect_candidate=setup)
        return original(rows, context)

    monkeypatch.setattr(app, "evaluate_candles", evaluate)
    history = candles()
    history += [dict(candles(1)[0], timeframe="H1", ts=BASE - timedelta(hours=1)),
                dict(candles(1)[0], timeframe="H1", ts=BASE + timedelta(hours=1), close=1.101)]
    path = tmp_path / "audit.jsonl"
    result = module.replay(history, results_path=path)
    assert observed == [(BASE + timedelta(minutes=i + 1), i + 1) for i in range(4)]
    assert result["trades"][0]["opened_at"] >= result["trades"][0]["detected_at"]
    assert result["trades"][0]["exit_reason"] == "stop"
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["candles"] and records[0]["risk_state"]
    assert records[0]["decision"]["setup"]
    assert records[-1]["news_policy"].startswith("skipped")


def test_future_prices_do_not_change_prefix(monkeypatch):
    original = app.evaluate_candles
    monkeypatch.setattr(app, "evaluate_candles", lambda rows, ctx: original(rows, dict(ctx, detect_candidate=setup)))
    prefix = candles(3)
    a = module.replay(prefix)
    b = module.replay(prefix + [dict(candles(4)[-1], high=9, close=8)])
    assert a["decisions"] == b["decisions"][:3]


def test_llm_is_opt_in_and_separate(monkeypatch):
    original = app.evaluate_candles
    monkeypatch.setattr(app, "evaluate_candles", lambda rows, ctx: original(rows, dict(ctx, detect_candidate=setup)))
    calls = []

    def veto(context):
        calls.append(context)
        return dict(verdict="veto", conviction=1, reasoning="test")

    monkeypatch.setattr(app.reasoning, "review_setup", veto)
    mechanical = module.replay(candles())
    assert not calls
    reviewed = module.replay(candles(), llm=True)
    assert calls and mechanical["trades"] and not reviewed["trades"]
    assert reviewed["initial_equity"] == mechanical["initial_equity"]
    assert all(d["decision"]["reason"] == "llm_veto" for d in reviewed["decisions"])


def test_timezone_and_duplicate_validation():
    with pytest.raises(ValueError, match="timezone-aware"):
        module.replay([dict(candles(1)[0], ts=BASE.replace(tzinfo=None))])
    with pytest.raises(ValueError, match="conflicting"):
        module.replay(candles(1) + [dict(candles(1)[0], high=2)])


def test_metrics():
    result = module.metrics([dict(r_multiple=2), dict(r_multiple=-1), dict(r_multiple=0)])
    assert result["win_rate"] == pytest.approx(1 / 3)
    assert result["expectancy_r"] == pytest.approx(1 / 3)
    assert result["average_rr"] == 2


def test_threshold_preserves_live_floor_and_rejects_below_sweep(monkeypatch):
    original = app.evaluate_candles

    def eight_point_setup(rows, context):
        candidate = setup(rows, context)
        candidate["factors"]["entry_in_ote_zone"] = False
        candidate["factors"]["fvg_present_unmitigated"] = False
        return candidate

    monkeypatch.setattr(app, "evaluate_candles", lambda rows, ctx: original(rows, dict(ctx, detect_candidate=eight_point_setup)))
    assert module.replay(candles(), threshold=7)["effective_threshold"] == 8
    strict = module.replay(candles(), threshold=9)
    assert not strict["trades"]
    assert all(d["decision"]["reason"] == "score_below_sweep_threshold" for d in strict["decisions"])


def test_derived_h1_only_available_after_full_hour(monkeypatch):
    seen = []

    def evaluate(rows, context):
        seen.append((context["now"], context["htf_candles"]))
        return app.PipelineDecision(False, "test")

    monkeypatch.setattr(app, "evaluate_candles", evaluate)
    module.replay(candles(61))
    assert all(not higher for _, higher in seen[:59])
    assert len(seen[59][1]) == 1
    assert seen[59][1][0]["ts"] + timedelta(hours=1) == seen[59][0]
    seen.clear()
    module.replay(candles(61)[1:])
    assert all(not higher for _, higher in seen)


def test_parquet_loader_reads_phase_one_shape(tmp_path):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    pd.DataFrame(candles()).to_parquet(tmp_path / "EURUSD_M1.parquet", index=False)
    loaded = module.load_history(tmp_path)
    assert loaded == candles()
