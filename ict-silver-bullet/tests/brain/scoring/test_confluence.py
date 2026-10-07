from unittest.mock import Mock

import pytest

from brain.scoring import confluence


@pytest.fixture
def audit(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(confluence, "setup_logging", lambda _: logger)
    return logger


def complete():
    return dict.fromkeys(confluence.FACTOR_POINTS, True)


def test_full_score_and_audit(audit):
    result = confluence.score_setup(complete(), True)
    assert result == {"score": 10, "max_score": 10, "reason": None,
                      "breakdown": confluence.FACTOR_POINTS}
    audit.info.assert_called_once()
    assert audit.info.call_args.args[1:] == (10, None, result["breakdown"])


@pytest.mark.parametrize("name,points", confluence.FACTOR_POINTS.items())
def test_each_factor(name, points, audit):
    flags = complete()
    flags[name] = 0
    result = confluence.score_setup(flags, True)
    assert result["score"] == (0 if name == "inside_killzone" else 10 - points)
    assert result["breakdown"][name] == 0
    audit.info.assert_called_once()


def test_outside_killzone_does_not_read_other_fields(audit):
    class GatesOnly(dict):
        def get(self, key, default=None):
            assert key == "inside_killzone"
            return False
    result = confluence.score_setup(GatesOnly(), False)
    assert result["reason"] == "outside_killzone"
    assert result["score"] == 0
    assert not any(result["breakdown"].values())
    audit.info.assert_called_once()


@pytest.mark.parametrize("news", [False, None])
def test_news_gate_preserves_scored_breakdown(news, audit):
    result = confluence.score_setup(complete(), news)
    assert result["score"] == 0
    assert result["reason"] == "news_blackout"
    assert sum(result["breakdown"].values()) == 10
    audit.info.assert_called_once()


def test_numeric_flags_and_embedded_news(audit):
    flags = dict.fromkeys(confluence.FACTOR_POINTS, 1)
    flags["no_high_impact_news_next_15min"] = True
    assert confluence.score_setup(flags)["score"] == 10
    assert confluence.score_setup(flags, False)["score"] == 0


def test_missing_factors_fail_closed(audit):
    assert confluence.score_setup({})["reason"] == "outside_killzone"
    result = confluence.score_setup({"inside_killzone": True}, True)
    assert result["score"] == 2
