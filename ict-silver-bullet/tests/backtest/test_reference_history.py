from datetime import date, datetime, timezone
import lzma
import struct
from urllib.error import HTTPError
from unittest.mock import Mock

import pytest

from backtest import pull_reference as reference
from backtest.replay import ask_quotes, ResearchConfig, session_momentum_replay
from tests.brain.detection.test_session_momentum import sessions

DAY = date(2024, 10, 16)


def packed(records):
    return lzma.compress(b"".join(struct.pack(">5If", *row) for row in records))


def test_decode_actual_format_price_scales_utc_and_no_gap_filling():
    rows = reference.decode(packed([(0,110000,110002,109999,110005,1.),
                                     (120,110002,110003,110001,110006,2.)]), "EURUSD", DAY)
    assert rows[0]["open"] == 1.1 and rows[0]["high"] == 1.10005
    assert rows[0]["ts"] == datetime(2024,10,16,tzinfo=timezone.utc)
    assert (rows[1]["ts"]-rows[0]["ts"]).total_seconds() == 120
    jpy = reference.decode(packed([(0,150000,150002,149999,150005,1.)]), "USDJPY", DAY)
    assert jpy[0]["open"] == 150.
    assert "/2024/09/16/" in reference.daily_url("EURUSD", DAY, "BID")


def test_invalid_and_zero_activity_records_are_not_synthetic_training_data():
    assert not reference.decode(packed([(0,110000,110000,110000,110000,0.)]), "EURUSD", DAY)
    with pytest.raises(ValueError, match="truncated"):
        reference.decode(lzma.compress(b"bad"), "EURUSD", DAY)
    with pytest.raises(ValueError, match="duplicate"):
        reference.decode(packed([(0,110000,110002,109999,110005,1.)]*2), "EURUSD", DAY)
    with pytest.raises(ValueError, match="OHLC"):
        reference.decode(packed([(0,110000,110002,110004,110005,1.)]), "EURUSD", DAY)


def test_bid_ask_alignment_preserves_missing_minutes_and_rejects_negative_spread():
    bid = reference.decode(packed([(0,110000,110002,109999,110005,1.),
                                    (60,110002,110003,110001,110006,1.)]), "EURUSD", DAY)
    ask = [dict(bid[0], **{k:bid[0][k]+.0001 for k in ("open","high","low","close")})]
    matched = reference.merge_sides(bid, ask)
    assert len(matched) == 1 and matched[0]["ask_open"] == 1.1001
    with pytest.raises(ValueError, match="Ask below Bid"):
        reference.merge_sides(bid, [dict(ask[0], close=1.)])


def test_rate_limit_is_respected_and_not_treated_as_missing_history():
    error = HTTPError("https://example.test", 429, "limited", {"Retry-After":"75"}, None)
    opener = Mock(side_effect=error)
    waits = []
    with pytest.raises(HTTPError):
        reference.fetch("https://example.test", attempts=2, opener=opener, sleeper=waits.append)
    assert waits == [60, 15]
    missing = Mock(side_effect=HTTPError("https://example.test",404,"missing",{},None))
    assert reference.fetch("https://example.test", opener=missing) == b""


def test_extended_outage_has_capped_backoff_and_visible_retries(caplog):
    error = HTTPError("https://example.test", 503, "unavailable", {}, None)
    opener = Mock(side_effect=error)
    waits = []
    with pytest.raises(HTTPError):
        reference.fetch("https://example.test", attempts=12, opener=opener, sleeper=waits.append)
    assert opener.call_count == 12
    assert waits == [60]*11
    assert "retry 12/12 in 60.0s" in caplog.text
    with pytest.raises(ValueError, match="positive"):
        reference.fetch("https://example.test", attempts=0, opener=opener)


def test_export_caches_valid_payloads_and_records_provenance_without_network(tmp_path):
    bid = packed([(0,110000,110002,109999,110005,1.)])
    ask = packed([(0,110010,110012,110009,110015,1.)])
    fetcher = Mock(side_effect=lambda url: ask if "ASK" in url else bid)
    report = reference.export(["EURUSD"], DAY, date(2024,10,17), tmp_path, fetcher=fetcher, pause=0)
    assert report["pairs"]["EURUSD"]["rows"] == 1
    assert len(report["pairs"]["EURUSD"]["files"]) == 2
    assert (tmp_path/"EURUSD_M1.parquet").exists()
    reference.export(["EURUSD"], DAY, date(2024,10,17), tmp_path,
                     fetcher=Mock(side_effect=AssertionError("must use cache")), pause=0)
    assert fetcher.call_count == 2


def test_historical_ask_used_for_fills_and_spread_stress_does_not_look_ahead():
    from dataclasses import replace
    rows, opening = sessions()
    for row in rows:
        row.update({"ask_"+field:row[field]+.00015 for field in ("open","high","low","close")})
    report = session_momentum_replay(rows, decision_start=opening)
    assert report["trades"][0]["entry"] == pytest.approx(rows[14*240+30]["ask_open"]+.00001)
    sample = dict(rows[14*240+30])
    sample.update(ask_close=sample["close"]+.001, ask_high=sample["high"]+.002)
    stressed = ask_quotes(sample, replace(ResearchConfig(), historical_spread_multiplier=2))
    assert stressed["open"] == pytest.approx(sample["ask_open"]+.00015)
    with pytest.raises(ValueError, match="partial"):
        ask_quotes({k:v for k,v in sample.items() if k != "ask_low"}, ResearchConfig())
