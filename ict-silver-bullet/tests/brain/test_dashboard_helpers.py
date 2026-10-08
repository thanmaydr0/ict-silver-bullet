"""Dashboard contract, transforms, sessions and API. Every service read is mocked."""
from datetime import datetime, timedelta
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from brain import dashboard_data as d
from brain.dashboard_api import create_dashboard
from brain.dashboard_auth import COOKIE, TTL_SECONDS, LoginThrottled, Sessions
from brain.dashboard_models import DashboardSnapshot

NOW = datetime(2026, 10, 7, 14, 5, tzinfo=ZoneInfo("UTC"))
CONFIG = SimpleNamespace(DASHBOARD_USERNAME="operator", DASHBOARD_PASSWORD="test-only-password",
                         DASHBOARD_HOST="127.0.0.1", DASHBOARD_PORT=7860)


@pytest.fixture(autouse=True)
def mock_runtime_file(monkeypatch):
    monkeypatch.setattr("brain.dashboard_api.dotenv_values",lambda *a,**k: {})


def reader():
    r = Mock()
    r.rows.return_value = []
    r.history.return_value = []
    r.candles.return_value = []
    return r


def test_empty_snapshot_and_utc_contract():
    c = d.Collector(reader(), pairs=["EURUSD"], now=lambda: NOW)
    c.collect()
    result = c.snapshot()
    assert result.positions.status == result.trades.status == result.risk.status == "empty"
    assert result.news.data.blackout[0].blocked is False
    assert result.killzone.state == "armed"
    assert result.generated_at.utcoffset() == timedelta(0)
    assert result.equity.data.points == []


def test_equity_limits_missing_and_peak_retention():
    rows = [{"ts": NOW.isoformat(), "equity": 9700, "daily_dd_pct": .03, "overall_dd_pct": .03}]
    e = d.equity_data(rows, rows[0])
    assert e.points[0].daily_limit == pytest.approx(9600)
    assert e.points[0].overall_limit == pytest.approx(9400)
    assert d.equity_data([{"ts": NOW, "equity": 10000}], None).points[0].daily_limit is None
    many = [{"ts": NOW+timedelta(seconds=i), "equity": 9500 if i==1033 else 10000} for i in range(5000)]
    reduced = d.equity_data(many, None).points
    assert len(reduced) <= 1000
    assert min(p.equity for p in reduced) == 9500
    assert reduced[0].ts == NOW and reduced[-1].ts == many[-1]["ts"]


def test_realized_zero_actual_fill_rr_and_floating_unavailable():
    t = d.trade_data([{"entry_fill": 1.11, "realized_r": 0, "signals": {"entry":1.1,"stop_loss":1.09,"take_profit":1.15}}])[0]
    assert t.rr == pytest.approx(2)
    assert t.entry == 1.11 and t.realized_r == 0
    assert d.position_data([{"realized_usd": 42}])[0].floating_pnl is None
    assert d.position_data([{"floating_pnl": 0}])[0].floating_pnl == 0
    assert d.trade_data([{"entry":1,"stop_loss":1,"take_profit":2}])[0].rr is None


def test_news_recent_scope_and_invalid_time():
    event = {"title":"CPI","currency":"USD","impact":"high","scheduled_at":NOW-timedelta(minutes=5)}
    n = d.news_data([event],["EURUSD","EURGBP"],NOW)
    assert [b.blocked for b in n.blackout] == [True,False]
    with pytest.raises(ValueError):
        d.news_data([{**event,"scheduled_at":"bad"}],["EURUSD"],NOW)


def test_setup_evidence_and_no_future_candles():
    candle = {"ts":NOW,"open":1,"high":3,"low":.5,"close":2}
    signal = {"id":9,"pair":"EURUSD","detected_at":NOW,"entry":1,"setup":{"candles":[candle,{**candle,"ts":NOW+timedelta(minutes=5)}],"fvg":{"bottom":1,"top":2},"ob":{"low":.5,"high":1},"mss":{"level":2},"sweep":{"wick_extreme":.5}}}
    setup = d.setup_data(signal, [])
    assert len(setup.candles) == 1 and len(setup.overlays) == 5
    assert setup.missing_evidence == [] and setup.source == "recorded"
    plain = d.setup_data({"pair":"EURUSD","detected_at":NOW},[candle])
    assert plain.source == "historical_context" and len(plain.missing_evidence) == 4


def test_risk_thresholds_and_invalid_numbers():
    risk = d.risk_data({"daily_dd_pct": .07, "overall_dd_pct":"nan"})
    assert risk.daily.hard == d.HARD_DAILY_DD_PCT
    assert risk.overall.soft == d.SOFT_OVERALL_DD_PCT and risk.overall.value is None
    with pytest.raises(ValidationError):
        d.account({"ts":NOW,"equity":float("inf")})
    with pytest.raises(ValueError):
        d.utc(datetime(2026,1,1))
    assert d.utc("2026-10-07T19:35:00+05:30") == NOW


def test_partial_failure_keeps_last_good_without_hiding_new_empty():
    r = reader()
    def rows(table, **kwargs):
        if table == "equity_snapshots":
            return [{"ts":NOW,"equity":10000,"daily_dd_pct":0,"overall_dd_pct":0}]
        if table == "trades" and kwargs.get("status"):
            return [{"id":1,"floating_pnl":0}]
        return []
    r.rows.side_effect = rows
    c = d.Collector(r, pairs=[], now=lambda:NOW)
    c.collect()
    assert c.snapshot().positions.data[0].floating_pnl == 0
    def failing(table, **kwargs):
        if table == "trades" and kwargs.get("status"):
            raise RuntimeError("secret must not escape")
        return rows(table, **kwargs)
    r.rows.side_effect = failing
    c.collect()
    snapshot = c.snapshot()
    assert snapshot.positions.status == "stale" and snapshot.positions.data[0].id == 1
    assert snapshot.trades.status == "empty" and snapshot.risk.status == "ready"
    assert "secret" not in snapshot.model_dump_json()
    r.rows.side_effect = lambda *a, **k: []
    c.collect()
    assert c.snapshot().positions.status == "empty" and c.snapshot().positions.data == []


def test_history_cache_refresh_failure_and_source_age():
    clock=[NOW]
    r=reader()
    r.rows.side_effect=lambda table,**k: [{"ts":NOW,"equity":10000}] if table=="equity_snapshots" else []
    c=d.Collector(r,pairs=[],now=lambda:clock[0])
    c.collect()
    clock[0]+=timedelta(seconds=10)
    c.collect()
    assert r.history.call_count == 1
    clock[0]+=timedelta(seconds=60)
    r.history.side_effect=RuntimeError("offline")
    c.collect()
    assert c.snapshot().equity.status == "stale" and c.snapshot().equity.data.latest.equity == 10000
    assert c.snapshot().risk.status == "stale"


def test_strict_reader_rejects_missing_and_queries_recent_news():
    query=Mock()
    for name in ("table","select","order","gte","lte","limit"):
        getattr(query,name).return_value=query
    r=d.SupabaseReader();r.client=query
    query.execute.return_value.data=None
    with pytest.raises(RuntimeError):r.rows("trades")
    query.execute.return_value.data=[]
    r.rows("news_events",field="scheduled_at",since=NOW-timedelta(minutes=15),until=NOW+timedelta(hours=24))
    query.gte.assert_called_with("scheduled_at",(NOW-timedelta(minutes=15)).isoformat())


def test_sessions_expiry_revocation_and_throttling():
    clock=[NOW]
    sessions=Sessions("user","pass",now=lambda:clock[0])
    token,_=sessions.login("user","pass","ip")
    assert sessions.check(token).username == "user"
    next_token,_=sessions.login("user","pass","ip",previous=token)
    assert sessions.check(token) is None
    clock[0]+=timedelta(seconds=TTL_SECONDS)
    assert sessions.check(next_token) is None
    for _ in range(10):assert sessions.login("user","wrong","ip") is None
    with pytest.raises(LoginThrottled):sessions.login("user","pass","ip")
    clock[0]+=timedelta(minutes=5)
    token,_=sessions.login("user","pass","ip")
    sessions.logout(token)
    assert sessions.check(token) is None


@pytest.fixture
def api(tmp_path):
    (tmp_path/"assets").mkdir()
    (tmp_path/"assets"/"app-hash.js").write_text("fixture",encoding="utf-8")
    (tmp_path/"index.html").write_text('<html><script src="/assets/app-hash.js"></script></html>',encoding="utf-8")
    build_id=sha256((tmp_path/"index.html").read_bytes()).hexdigest()[:16]
    (tmp_path/"build.json").write_text(json.dumps({"schema_version":1,"build_id":build_id}),encoding="utf-8")
    c=Mock()
    c.snapshot.return_value=d.Collector(reader(),pairs=[],now=lambda:NOW).snapshot()
    return create_dashboard(CONFIG,collector=c,dist=tmp_path),c


def login(client):
    return client.post("/api/v1/auth/login",json={"username":"operator","password":"test-only-password"},headers={"Origin":"http://testserver"})


def test_api_protected_login_logout_and_no_public_docs(api):
    app,c=api
    with TestClient(app) as client:
        assert client.get("/api/v1/dashboard").status_code == 401
        assert client.get("/api/v1/auth/session").status_code == 401
        assert client.get("/docs").status_code == 404
        assert client.get("/api/missing").status_code == 404
        result=login(client)
        assert result.status_code == 200
        cookie=result.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie
        assert "password" not in result.text
        token=client.cookies.get(COOKIE)
        assert client.get("/api/v1/dashboard").json()["schema_version"] == 1
        assert client.post("/api/v1/auth/logout",headers={"Origin":"http://testserver"}).status_code == 204
        assert client.get("/api/v1/dashboard",headers={"Cookie":f"{COOKIE}={token}"}).status_code == 401
    c.start.assert_called_once();c.close.assert_called_once()


def test_origin_throttle_invalid_credentials_and_secure_cookie(api,monkeypatch):
    app,_=api
    with TestClient(app) as client:
        body={"username":"operator","password":"wrong"}
        for origin in (None,"https://evil.example","http://testserver.evil"):
            assert client.post("/api/v1/auth/login",json=body,headers={"Origin":origin} if origin else {}).status_code == 403
        for _ in range(10):
            assert client.post("/api/v1/auth/login",json=body,headers={"Origin":"http://testserver"}).status_code == 401
        assert login(client).status_code == 429
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE","true")
    app=create_dashboard(CONFIG,collector=Mock(),development=True)
    with TestClient(app,base_url="https://testserver") as client:
        result=client.post("/api/v1/auth/login",json={"username":"operator","password":"test-only-password"},headers={"Origin":"https://testserver"})
        assert "Secure" in result.headers["set-cookie"]


def test_build_serving_cache_health_missing_and_credentials(api,tmp_path):
    app,_=api
    with TestClient(app) as client:
        assert client.get("/").status_code == client.get("/dashboard").status_code == 200
        assert client.get("/").headers["cache-control"] == "no-store"
        assert "immutable" in client.get("/assets/app-hash.js").headers["cache-control"]
        health=client.get("/healthz").json()
        assert health["status"] == "ok" and health["schema_version"] == 1 and len(health["build_id"]) == 16
    with pytest.raises(RuntimeError,match="build missing"):
        create_dashboard(CONFIG,dist=tmp_path/"missing")
    with pytest.raises(RuntimeError,match="required"):
        create_dashboard(SimpleNamespace(DASHBOARD_USERNAME="",DASHBOARD_PASSWORD=""),development=True)


def test_checked_in_frontend_schema_matches_python():
    schema=Path(__file__).resolve().parents[2]/"frontend"/"src"/"dashboard.schema.json"
    assert json.loads(schema.read_text(encoding="utf-8")) == DashboardSnapshot.model_json_schema(mode="serialization")


def test_missing_hashed_asset_and_index_mismatch_refuse_startup(api,tmp_path):
    # API fixture already created a validated bundle; damage it after creation.
    (tmp_path/"assets"/"app-hash.js").unlink()
    with pytest.raises(RuntimeError,match="build missing or invalid"):
        create_dashboard(CONFIG,dist=tmp_path)
    (tmp_path/"assets"/"app-hash.js").write_text("fixture",encoding="utf-8")
    (tmp_path/"index.html").write_text("changed index",encoding="utf-8")
    with pytest.raises(RuntimeError,match="build missing or invalid"):
        create_dashboard(CONFIG,dist=tmp_path)


def test_proxy_options_refuse_wildcard(monkeypatch):
    from brain.dashboard_api import runtime_options
    monkeypatch.setenv("DASHBOARD_TRUSTED_PROXY_IP","*")
    with pytest.raises(RuntimeError,match="explicit proxy IP"):runtime_options()
    monkeypatch.setenv("DASHBOARD_TRUSTED_PROXY_IP","127.0.0.1")
    monkeypatch.setenv("DASHBOARD_COOKIE_SECURE","true")
    assert runtime_options() == (True,"127.0.0.1")


def test_collector_does_not_overlap_reads():
    r=reader();c=d.Collector(r,pairs=[],now=lambda:NOW)
    c._collect_lock.acquire()
    try:c.collect()
    finally:c._collect_lock.release()
    r.rows.assert_not_called()


def test_history_overflow_is_unavailable_not_truncated(monkeypatch):
    query=Mock()
    for name in ("table","select","gte","lte","order","range"):
        getattr(query,name).return_value=query
    query.execute.return_value.data=[{}]*1000
    query.execute.return_value.count=50100
    r=d.SupabaseReader();r.client=query
    monkeypatch.setattr(d,"monotonic",lambda:0)
    with pytest.raises(RuntimeError,match="exceeds dashboard bound"):r.history(NOW)
    assert query.execute.call_count == 50


def test_pagination_handles_lower_project_row_cap_and_incomplete_position_reads():
    query=Mock()
    for name in ("table","select","gte","lte","order","range","limit"):
        getattr(query,name).return_value=query
    query.execute.side_effect=[SimpleNamespace(data=[{"id":1},{"id":2}],count=3),SimpleNamespace(data=[{"id":3}],count=3)]
    r=d.SupabaseReader();r.client=query
    assert [row["id"] for row in r.history(NOW)] == [1,2,3]
    assert query.range.call_args_list[1].args == (2,1001)
    query.execute.side_effect=None
    query.execute.return_value=SimpleNamespace(data=[{"id":1}],count=2)
    with pytest.raises(RuntimeError,match="Incomplete"):r.rows("trades",complete=True)


def test_main_validates_auth_first_and_preserves_entrypoint(monkeypatch):
    from brain import dashboard
    import uvicorn
    monkeypatch.setattr(dashboard,"setup_logging",Mock())
    monkeypatch.setattr(dashboard,"load_dashboard_config",Mock(side_effect=RuntimeError("missing credentials")))
    with pytest.raises(RuntimeError,match="credentials"):dashboard.main()
    monkeypatch.setattr(dashboard,"load_dashboard_config",Mock(return_value=CONFIG))
    build=Mock(return_value=Mock())
    monkeypatch.setattr("brain.dashboard_api.create_dashboard",build)
    run=Mock();monkeypatch.setattr(uvicorn,"run",run)
    dashboard.main()
    assert run.call_args.kwargs["host"] == "127.0.0.1" and run.call_args.kwargs["workers"] == 1
