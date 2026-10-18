import json

import pytest

from backtest.replay import ResearchConfig, load_spread_profile
from brain.pairs import pair_spec


@pytest.mark.parametrize(
    "pair,price,expected",
    [("USDCAD", 1.25, 8.0), ("USDCHF", 0.8, 12.5)],
)
def test_usd_base_pip_value_converts_quote_pips_to_usd(pair, price, expected):
    result = pair_spec(pair, price)
    assert result["pip_size"] == 0.0001
    assert result["pip_value_per_lot"] == pytest.approx(expected)


def test_pair_spread_profile_loads_tick_audit_and_fails_closed(tmp_path):
    path = tmp_path / "tick-audit.json"
    path.write_text(json.dumps({"spreads": [
        {"symbol": "EURUSD", "p95_pips": 0.4},
        {"symbol": "USDCHF", "p95_pips": 0.8},
    ]}), encoding="utf-8")

    config = ResearchConfig(spread_pips=load_spread_profile(path))
    assert config.spread_for("EURUSD") == pytest.approx(0.4)
    assert config.spread_for("USDCHF") == pytest.approx(0.8)
    with pytest.raises(ValueError, match="no entry"):
        config.spread_for("GBPUSD")


def test_invalid_pair_spread_profiles_are_rejected():
    for profile in ({}, {"EURUSD": -0.1}, {"EURUSD": float("inf")}):
        with pytest.raises(ValueError, match="spread profile"):
            ResearchConfig(spread_pips=profile)
