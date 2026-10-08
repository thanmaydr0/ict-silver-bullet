"""Loopback-only browser fixture. Deterministic test data; never calls a service."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from brain.dashboard_api import create_dashboard
from brain.dashboard_data import Collector


class Reader:
    def rows(self, table, **kwargs):
        now = datetime.now(ZoneInfo("UTC"))
        if table == "equity_snapshots":
            return [{"ts":now,"equity":24250,"balance":24500,"daily_dd_pct":.018,"overall_dd_pct":.03}]
        if table == "trades":
            if kwargs.get("status"):
                return [{"id":1,"leg":"runner","lots":.2,"signals":{"pair":"EURUSD","direction":"buy"}}]
            return [{"id":i,"opened_at":now-timedelta(hours=i+1),"status":"closed","entry_fill":1.1042,
                     "realized_r":1.5 if i%2 else 0,"signals":{"pair":"EURUSD" if i%2 else "GBPUSD",
                     "direction":"buy","entry":1.104,"stop_loss":1.102,"take_profit":1.108,"confluence_score":9}} for i in range(6)]
        if table == "news_events":
            return [{"title":"US inflation report (fixture)","currency":"USD","impact":"high","scheduled_at":now+timedelta(minutes=10)},
                    {"title":"Policy rate decision (fixture)","currency":"EUR","impact":"high","scheduled_at":now+timedelta(hours=3)}]
        if table == "signals":
            return [{"id":9,"pair":"EURUSD","direction":"buy","detected_at":now,"entry":1.105,"stop_loss":1.102,"take_profit":1.110,
                     "setup":{"fvg":{"bottom":1.1035,"top":1.1043},"ob":{"low":1.102,"high":1.103},"mss":{"level":1.1048},"sweep":{"wick_extreme":1.1018}}}]
        return []

    def history(self, now):
        import math
        return [{"ts":now-timedelta(minutes=(143-i)*10),"equity":24000+i*1.8+math.sin(i/8)*100,
                 "daily_dd_pct":.018,"overall_dd_pct":.03} for i in range(144)]

    def candles(self, signal):
        import math
        now=signal["detected_at"]
        return [{"ts":now-timedelta(minutes=(95-i)*5),"open":1.102+i*.000035+math.sin(i/6)*.0005,
                 "close":1.102+i*.000035+math.sin(i/6)*.0005+(.00015 if i%3 else -.0002),
                 "high":1.102+i*.000035+math.sin(i/6)*.0005+.0004,
                 "low":1.102+i*.000035+math.sin(i/6)*.0005-.0004} for i in range(96)]


if __name__ == "__main__":
    import uvicorn
    c=Collector(Reader(),pairs=["EURUSD","GBPUSD"])
    c.collect()
    config=SimpleNamespace(DASHBOARD_USERNAME="fixture-user", DASHBOARD_PASSWORD="fixture-password")
    with patch("brain.dashboard_api.dotenv_values",return_value={}):
        app=create_dashboard(config,collector=c)
    uvicorn.run(app,host="127.0.0.1",port=8791,proxy_headers=False,access_log=False)
