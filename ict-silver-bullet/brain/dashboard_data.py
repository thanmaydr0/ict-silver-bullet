"""Strict, bounded reads and shared snapshots; failures never mean zero trades."""
from datetime import datetime, timedelta
import logging
import math
from threading import Event, Lock, Thread
from time import monotonic
from zoneinfo import ZoneInfo

from brain.config import load_config
from brain.dashboard_models import (Account, Blackout, Candle, DashboardSnapshot,
    EquityData, EquityPoint, Gauge, Killzone, NewsData, NewsEvent, Overlay, Panel,
    Position, RiskData, SetupData, Trade)
from brain.news.blackout import is_in_news_blackout
from brain.risk.circuit_breakers import (SOFT_DAILY_DD_PCT, HARD_DAILY_DD_PCT,
    SOFT_OVERALL_DD_PCT, HARD_OVERALL_DD_PCT)
from brain.scheduler.killzone_clock import current_killzone, next_killzone_open, killzone_close

UTC = ZoneInfo("UTC")
LOG = logging.getLogger(__name__)


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ValueError("Aware timestamp required")
    return value.astimezone(UTC)


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def timestamp(value):
    try:
        return utc(value)
    except (ValueError, TypeError):
        return None


def window(now):
    active = current_killzone(now)
    name, opening = next_killzone_open(now)
    return Killzone(state="armed" if active else "idle", active_window=active,
        closes_at=killzone_close(active, now) if active else None,
        next_window=name, opens_at=opening)


def account(row):
    if not row:
        return None
    return Account(ts=utc(row["ts"]), equity=row["equity"], balance=number(row.get("balance")),
        daily_dd_pct=number(row.get("daily_dd_pct")), overall_dd_pct=number(row.get("overall_dd_pct")))


def limit_line(equity, dd, limit):
    # NOTE: Recover persisted baselines instead of assuming the first visible point
    # is a day/account opening. Missing or impossible drawdown remains unavailable.
    return equity / (1 - dd) * (1 - limit) if dd is not None and dd < 1 else None


def equity_data(rows, latest):
    if latest:
        latest_at = utc(latest["ts"])
        rows = [r for r in rows if utc(r["ts"]) != latest_at] + [latest]
    points = []
    for row in rows:
        a = account(row)
        points.append(EquityPoint(ts=a.ts, equity=a.equity,
            daily_limit=limit_line(a.equity, a.daily_dd_pct, .04),
            overall_limit=limit_line(a.equity, a.overall_dd_pct, .06)))
    points.sort(key=lambda p: p.ts)
    if len(points) > 1000:
        # NOTE: Time buckets retain both equity extrema and limit-line extrema;
        # the full history remains in the server cache, not every browser poll.
        stride = math.ceil(len(points) / 100)
        reduced = []
        for start in range(0, len(points), stride):
            batch = points[start:start+stride]
            indices = {0, len(batch)-1}
            for field in ("equity", "daily_limit", "overall_limit"):
                known = [i for i, p in enumerate(batch) if getattr(p, field) is not None]
                if known:
                    indices.update((min(known, key=lambda i: getattr(batch[i], field)),
                                    max(known, key=lambda i: getattr(batch[i], field))))
                    if field != "equity":
                        positive = [i for i in known if getattr(batch[i], field) > 0]
                        if positive:
                            indices.add(min(positive, key=lambda i: batch[i].equity / getattr(batch[i], field)))
            reduced.extend(batch[i] for i in sorted(indices))
        points = reduced
    return EquityData(points=points, latest=account(latest))


def risk_data(row):
    return RiskData(daily=Gauge(value=number(row.get("daily_dd_pct")), soft=SOFT_DAILY_DD_PCT,
        hard=HARD_DAILY_DD_PCT, limit=.04), overall=Gauge(value=number(row.get("overall_dd_pct")),
        soft=SOFT_OVERALL_DD_PCT, hard=HARD_OVERALL_DD_PCT, limit=.06))


def join_trade(row):
    result = dict(row)
    signal = result.pop("signals", None)
    if isinstance(signal, list):
        signal = signal[0] if signal else None
    if isinstance(signal, dict):
        result.update(signal)
    return result


def trade_data(rows):
    trades = []
    for raw in rows:
        r = join_trade(raw)
        entry = number(r.get("entry_fill"))
        if entry is None:
            entry = number(r.get("entry"))
        sl, tp = number(r.get("stop_loss")), number(r.get("take_profit"))
        rr = abs(tp - entry) / abs(entry - sl) if None not in (entry, sl, tp) and entry != sl else None
        trades.append(Trade(id=r.get("id"), timestamp=timestamp(r.get("opened_at")),
            pair=r.get("pair"), direction=r.get("direction"), entry=entry, stop_loss=sl,
            take_profit=tp, confluence_score=number(r.get("confluence_score")), rr=rr,
            status=r.get("status"), realized_r=number(r.get("realized_r"))))
    return trades


def position_data(rows):
    return [Position(id=r.get("id"), pair=r.get("pair"), direction=r.get("direction"),
        leg=str(r["leg"]) if r.get("leg") is not None else None, lots=number(r.get("lots")),
        floating_pnl=number(r.get("floating_pnl"))) for r in map(join_trade, rows)]


def news_data(rows, pairs, now):
    # Invalid event times reject the panel so the blackout cannot appear clear.
    events = [NewsEvent(title=r["title"], currency=r["currency"], scheduled_at=utc(r["scheduled_at"]))
              for r in rows if str(r.get("impact", "")).lower() == "high"]
    return NewsData(events=sorted(events, key=lambda e: e.scheduled_at),
        blackout=[Blackout(pair=p, blocked=is_in_news_blackout(now, p, rows)) for p in pairs])


def setup_data(signal, candles):
    evidence = signal.get("setup") or {}
    recorded = isinstance(evidence.get("candles"), list)
    detected = utc(signal["detected_at"])
    points = [Candle(ts=utc(r["ts"]), **{k: r[k] for k in ("open", "high", "low", "close")})
              for r in (evidence["candles"] if recorded else candles) if utc(r["ts"]) <= detected]
    overlays, missing = [], []
    for key, label, low, high in (("fvg", "FVG", "bottom", "top"), ("ob", "Order block", "low", "high"),
                                  ("mss", "MSS", "level", "level"), ("sweep", "Sweep", "wick_extreme", "wick_extreme")):
        raw = evidence.get(key) or {}
        items = raw if isinstance(raw, list) else [raw]
        found = False
        for item in items:
            if not isinstance(item, dict):
                continue
            bottom, top = number(item.get(low)), number(item.get(high))
            if key == "sweep" and bottom is None:
                bottom = top = number(item.get("level"))
            if bottom is not None and top is not None:
                found = True
                overlays.append(Overlay(label=label, kind="line" if low == high else "band", bottom=bottom, top=top))
        if not found:
            missing.append(label)
    for key, label in (("entry", "Entry"), ("stop_loss", "Stop loss"), ("take_profit", "Take profit")):
        value = number(signal.get(key))
        if value is not None:
            overlays.append(Overlay(label=label, kind="line", bottom=value, top=value))
    return SetupData(signal_id=signal.get("id"), pair=signal["pair"], direction=signal.get("direction"),
        detected_at=detected, candles=sorted(points, key=lambda c: c.ts)[-100:], overlays=overlays,
        missing_evidence=missing, source="recorded" if recorded else "historical_context")


class SupabaseReader:
    """Separate dashboard timeout; existing fault-tolerant DB signatures stay intact."""
    def __init__(self):
        self.client = None

    def _client(self):
        if self.client is None:
            from supabase import create_client
            from supabase.client import ClientOptions
            config = load_config()
            self.client = create_client(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY,
                options=ClientOptions(postgrest_client_timeout=5, storage_client_timeout=5,
                                      persist_session=False, auto_refresh_token=False))
        return self.client

    def rows(self, table, *, select="*", since=None, until=None, field="ts", limit=100, status=None, complete=False):
        query = self._client().table(table).select(select, count="exact" if complete else None).order(field, desc=True).order("id", desc=True)
        if since is not None:
            query = query.gte(field, since.isoformat())
        if until is not None:
            query = query.lte(field, until.isoformat())
        if status:
            query = query.eq("status", status)
        return self._execute(query.limit(limit), complete=complete)

    @staticmethod
    def _execute(query, complete=False):
        response = query.execute()
        data = response.data
        if not isinstance(data, list):
            raise RuntimeError("Missing database response")
        if complete and (not isinstance(response.count, int) or response.count != len(data)):
            raise RuntimeError("Incomplete database response")
        return data

    def history(self, now):
        query = self._client().table("equity_snapshots").select("*", count="exact").gte("ts", (now-timedelta(hours=24)).isoformat())
        query = query.lte("ts", now.isoformat()).order("ts").order("id")
        rows, deadline, offset = [], monotonic()+12, 0
        # NOTE: Bound pagination/time and flag overflow; do not silently truncate.
        for page in range(50):
            if monotonic() > deadline:
                raise TimeoutError("History deadline")
            response = query.range(offset, offset+999).execute()
            batch = response.data
            if not isinstance(batch, list) or not isinstance(response.count, int):
                raise RuntimeError("Missing history response/count")
            rows.extend(batch)
            offset += len(batch)
            # A project's API row cap may be below 1000. Advance by the actual
            # count instead of skipping rows or treating a capped page as final.
            if offset >= response.count:
                return rows
            if not batch:
                raise RuntimeError("Incomplete history response")
        raise RuntimeError("History exceeds dashboard bound")

    def candles(self, signal):
        query = self._client().table("candles").select("*").eq("pair", signal["pair"]).eq("timeframe", "M1")
        return self._execute(query.lte("ts", utc(signal["detected_at"]).isoformat()).order("ts", desc=True).limit(100))


class Collector:
    def __init__(self, reader=None, pairs=None, now=None):
        self.reader = reader or SupabaseReader()
        self.pairs = pairs
        self.now = now or (lambda: datetime.now(UTC))
        self._lock, self._collect_lock, self._stop = Lock(), Lock(), Event()
        self._thread = None
        self._panels = {key: Panel() for key in ("equity", "risk", "trades", "positions", "news", "setup")}
        self._history, self._history_at, self._collected_at = [], None, None

    def _read(self, key, fn, now):
        try:
            data = fn()
            empty = data is None or data == []
            if isinstance(data, EquityData):
                empty = not data.points and data.latest is None
            panel = Panel(status="empty" if empty else "ready", data=data, updated_at=now,
                          message="No data yet" if empty else None)
        except Exception as exc:
            LOG.warning("Dashboard %s unavailable (%s)", key, type(exc).__name__)
            with self._lock:
                previous = self._panels[key]
            panel = previous.model_copy(update={"status": "stale" if previous.updated_at else "unavailable",
                                                "message": "Read failed; retrying"})
        with self._lock:
            self._panels[key] = panel

    def collect(self):
        if not self._collect_lock.acquire(blocking=False):
            return
        try:
            now = self.now()
            latest = None
            def risk():
                nonlocal latest
                rows = self.reader.rows("equity_snapshots", limit=1)
                latest = rows[0] if rows else None
                if latest:
                    account(latest)
                return risk_data(latest) if latest else None
            self._read("risk", risk, now)
            if latest and self._panels["risk"].status == "ready":
                with self._lock:
                    self._panels["risk"].updated_at = utc(latest["ts"])
            select = "*,signals(pair,direction,confluence_score,entry,stop_loss,take_profit)"
            self._read("trades", lambda: trade_data(self.reader.rows("trades", select=select, field="opened_at")), now)
            def positions():
                rows = self.reader.rows("trades", select=select, field="opened_at", status="open", limit=1000, complete=True)
                if len(rows) >= 1000:
                    raise RuntimeError("Positions exceed dashboard bound")
                return position_data(rows)
            self._read("positions", positions, now)
            def news():
                pairs = self.pairs if self.pairs is not None else load_config().TRADED_PAIRS
                rows = self.reader.rows("news_events", field="scheduled_at", since=now-timedelta(minutes=15), until=now+timedelta(hours=24), limit=1000, complete=True)
                if len(rows) >= 1000:
                    raise RuntimeError("News exceeds dashboard bound")
                return news_data(rows, pairs, now)
            self._read("news", news, now)
            def setup():
                rows = self.reader.rows("signals", field="detected_at", limit=1)
                if not rows:
                    return None
                signal = rows[0]
                return setup_data(signal, [] if (signal.get("setup") or {}).get("candles") is not None else self.reader.candles(signal))
            self._read("setup", setup, now)
            def equity():
                if self._history_at is None or (now-self._history_at).total_seconds() >= 60:
                    rows = self.reader.history(now)
                    self._history, self._history_at = rows, now
                if latest is None and self._panels["risk"].status in ("stale", "unavailable"):
                    raise RuntimeError("Latest equity unavailable")
                return equity_data(self._history, latest)
            self._read("equity", equity, now)
            if latest and self._panels["equity"].status == "ready":
                with self._lock:
                    self._panels["equity"].updated_at = utc(latest["ts"])
            with self._lock:
                self._collected_at = now
        finally:
            self._collect_lock.release()

    def snapshot(self):
        now = self.now()
        with self._lock:
            panels = {k: v.model_copy(deep=True) for k, v in self._panels.items()}
            collected = self._collected_at
        for panel in panels.values():
            if panel.updated_at and (now-panel.updated_at).total_seconds() > 30:
                panel.status, panel.message = "stale", "Last successful read is old"
        return DashboardSnapshot(generated_at=now, collected_at=collected, killzone=window(now), **panels)

    def start(self):
        def run():
            while not self._stop.is_set():
                try:
                    self.collect()
                except Exception as exc:
                    LOG.warning("Dashboard collection failed (%s)", type(exc).__name__)
                self._stop.wait(10)
        self._thread = Thread(target=run, name="dashboard-collector", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
