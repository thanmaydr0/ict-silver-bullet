"""Authenticated, read-only dashboard; run separately with python -m brain.dashboard."""

from datetime import datetime, timedelta
from functools import lru_cache
from html import escape
import logging
from math import isfinite
from zoneinfo import ZoneInfo

from brain.config import load_dashboard_config, load_config
from brain.db import supabase_client as db
from brain.logging_setup import setup_logging
from brain.news.blackout import is_in_news_blackout
from brain.risk.circuit_breakers import (
    SOFT_DAILY_DD_PCT, HARD_DAILY_DD_PCT,
    SOFT_OVERALL_DD_PCT, HARD_OVERALL_DD_PCT,
)
from brain.scheduler.killzone_clock import current_killzone, next_killzone_open

UTC = ZoneInfo("UTC")
logger = logging.getLogger("brain")
TRADE_HEADERS = ["Timestamp (UTC)", "Pair", "Direction", "Entry", "SL", "TP",
                 "Confluence", "R:R", "Status", "Realized R"]
POSITION_HEADERS = ["Trade", "Pair", "Direction", "Leg", "Lots", "Floating P/L"]


def number(value):
    try:
        result = float(value)
        return result if isfinite(result) else None
    except (TypeError, ValueError):
        return None


def timestamp(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.utcoffset() is not None else None
    except (TypeError, ValueError):
        return None


def empty_figure(title, message="No data yet"):
    import plotly.graph_objects as go
    fig = go.Figure()
    fig.update_layout(title=title, template="plotly_white")
    fig.add_annotation(text=message, x=.5, y=.5, xref="paper", yref="paper", showarrow=False)
    return fig


def build_equity_figure(rows):
    """Recover each snapshot's true baselines from its persisted fractional DD."""
    import plotly.graph_objects as go
    valid = sorted((r for r in rows if timestamp(r.get("ts")) and number(r.get("equity")) is not None),
                   key=lambda r: timestamp(r["ts"]))
    if not valid:
        return empty_figure("Equity curve")
    if len(valid) > 2000:
        sampled = []
        size = (len(valid) + 499) // 500
        for start in range(0, len(valid), size):
            bucket = valid[start:start+size]
            keep = {0, len(bucket)-1,
                    min(range(len(bucket)), key=lambda i: number(bucket[i]["equity"])),
                    max(range(len(bucket)), key=lambda i: number(bucket[i]["equity"]))}
            # Also retain any risk peak even when changing baselines differ.
            for field in ("daily_dd_pct", "overall_dd_pct"):
                keep.add(max(range(len(bucket)), key=lambda i: number(bucket[i].get(field)) or 0))
            sampled.extend(bucket[i] for i in sorted(keep))
        valid = sampled
    x = [timestamp(r["ts"]) for r in valid]
    equity = [number(r["equity"]) for r in valid]
    fig = go.Figure(go.Scatter(x=x, y=equity, name="Equity", mode="lines"))
    # NOTE: DD baselines may reset/change, so limits are step lines per snapshot,
    # rather than misleading horizontal lines based on today's balance.
    for field, limit, label, color in (("daily_dd_pct", .04, "4% daily limit", "orange"),
                                        ("overall_dd_pct", .06, "6% overall limit", "red")):
        levels = []
        for r, value in zip(valid, equity):
            dd = number(r.get(field))
            levels.append(value / (1 - dd) * (1 - limit) if dd is not None and dd < 1 else None)
        fig.add_trace(go.Scatter(x=x, y=levels, name=label, mode="lines",
                                line=dict(color=color, dash="dash", shape="hv")))
    fig.update_layout(title="Equity curve (last 24 hours)", template="plotly_white",
                      xaxis_title="UTC", yaxis_title="Account currency", hovermode="x unified")
    return fig


def build_drawdown_gauge(value, daily=True):
    import plotly.graph_objects as go
    title = "Daily drawdown" if daily else "Overall drawdown"
    value = number(value)
    if value is None:
        return empty_figure(title)
    soft, hard, limit = ((SOFT_DAILY_DD_PCT, HARD_DAILY_DD_PCT, .04) if daily else
                         (SOFT_OVERALL_DD_PCT, HARD_OVERALL_DD_PCT, .06))
    maximum = max(limit * 1.25, value * 1.1)
    fig = go.Figure(go.Indicator(mode="gauge+number", value=value * 100,
        number={"suffix": "%"}, title={"text": title}, gauge={
            "axis": {"range": [0, maximum * 100], "tickvals": [0, soft*100, hard*100, limit*100],
                     "ticktext": ["0", f"Soft {soft*100:g}%", f"Hard {hard*100:g}%", f"Limit {limit*100:g}%"]},
            "bar": {"color": "red" if value >= hard else "steelblue"},
            "steps": [{"range": [0, soft*100], "color": "#d5efdf"},
                      {"range": [soft*100, hard*100], "color": "#ffe4aa"},
                      {"range": [hard*100, maximum*100], "color": "#ffc5c5"}],
            "threshold": {"value": hard*100, "line": {"color": "red", "width": 3}},
        }))
    fig.update_layout(template="plotly_white", height=300)
    return fig


def trade_rows(trades):
    result = []
    for t in trades:
        entry = number(t.get("entry_fill"))
        if entry is None:
            entry = number(t.get("entry"))
        sl, tp = number(t.get("stop_loss")), number(t.get("take_profit"))
        rr = abs(tp-entry)/abs(entry-sl) if None not in (entry, sl, tp) and entry != sl else None
        ts = timestamp(t.get("opened_at"))
        result.append([ts.isoformat() if ts else "Unavailable", t.get("pair"), t.get("direction"),
                       entry, sl, tp, t.get("confluence_score"), round(rr, 2) if rr is not None else None,
                       t.get("status"), number(t.get("realized_r"))])
    return result or [["No data yet"] + [None] * (len(TRADE_HEADERS)-1)]


def position_rows(trades):
    # NOTE: Current trades schema has no floating P/L. Never substitute realized
    # P/L or estimate account-currency P/L from FX prices without broker conversion.
    return [[t.get("id"), t.get("pair"), t.get("direction"), t.get("leg"), t.get("lots"),
             number(t.get("floating_pnl")) if number(t.get("floating_pnl")) is not None else
             "Unavailable (not reported)"] for t in trades] or [["No data yet"] + [None]*5]


def killzone_text(now):
    active = current_killzone(now)
    name, opening = next_killzone_open(now)
    seconds = int((opening-now).total_seconds())
    return (f"{'Armed — '+active+'; watching for a setup' if active else 'Idle'}\n\n"
            f"Next window: {name} in {seconds//3600:02}:{seconds%3600//60:02}:{seconds%60:02} "
            f"({opening.isoformat()})\n\nWindow status only; risk/news guards can block entries.")


def news_text(events, recent_events, pairs, now):
    high = sorted((e for e in events if str(e.get("impact", "")).lower() == "high"
                   and timestamp(e.get("scheduled_at")) and timestamp(e["scheduled_at"]) >= now),
                  key=lambda e: timestamp(e["scheduled_at"]))
    if high:
        event = high[0]
        minutes = (timestamp(event["scheduled_at"])-now).total_seconds()/60
        text = f"Next high-impact event: {escape(str(event.get('title', 'Untitled')))} ({escape(str(event.get('currency', '')))}) — {minutes:.1f} minutes"
    else:
        text = "No data yet — no high-impact events in the next 24 hours"
    if recent_events is None:
        return text + "\n\nBlackout: unknown (recent news read failed)"
    if not pairs:
        return text + "\n\nBlackout: unknown (pair configuration unavailable)"
    for pair in pairs:
        blocked = is_in_news_blackout(now, pair, recent_events + events)
        text += f"\n\n{pair}: {'blocked' if blocked else 'armed / clear'} (±15-minute news gate)"
    return text


def build_setup_figure(signal, candles):
    """Render persisted evidence only; never rerun detectors as an audit substitute."""
    import plotly.graph_objects as go
    if not signal:
        return empty_figure("Latest setup")
    evidence = signal.get("setup") or {}
    window = evidence.get("candles") or candles
    detected = timestamp(signal.get("detected_at"))
    valid = sorted((c for c in window if timestamp(c.get("ts")) and
                    (detected is None or timestamp(c["ts"]) <= detected) and
                    all(number(c.get(k)) is not None for k in ("open", "high", "low", "close"))),
                   key=lambda c: timestamp(c["ts"]))[-100:]
    if not valid:
        return empty_figure("Latest setup", "No data yet — signal OHLC window unavailable")
    fig = go.Figure(go.Candlestick(x=[timestamp(c["ts"]) for c in valid],
        **{k: [number(c[k]) for c in valid] for k in ("open", "high", "low", "close")}, name="OHLC"))
    for key, label, color in (("entry", "Entry", "blue"), ("stop_loss", "SL", "red"), ("take_profit", "TP", "green")):
        value = number(signal.get(key))
        if value is not None:
            fig.add_hline(y=value, line_color=color, annotation_text=label)
    missing = []
    # NOTE: Schema does not persist detector evidence or OHLC yet. Optional `setup`
    # JSON uses candles, fvg(top/bottom), ob(high/low), mss(level), sweep(level or
    # wick_extreme). Historical fallback candles are context, not recorded evidence.
    for key, label, color in (("fvg", "FVG", "green"), ("ob", "OB", "purple"),
                               ("mss", "MSS", "orange"), ("sweep", "Liquidity sweep", "brown")):
        items = evidence.get(key, signal.get(key))
        items = items if isinstance(items, list) else [items] if isinstance(items, dict) else []
        annotated = False
        for item in items:
            lo, hi = number(item.get("bottom", item.get("low"))), number(item.get("top", item.get("high")))
            level = number(item.get("level", item.get("wick_extreme")))
            if lo is not None and hi is not None:
                fig.add_hrect(y0=lo, y1=hi, fillcolor=color, opacity=.12, line_width=1, annotation_text=label)
                annotated = True
            elif level is not None:
                fig.add_hline(y=level, line_color=color, line_dash="dot", annotation_text=label)
                annotated = True
        if not annotated:
            missing.append(label)
    if missing:
        fig.add_annotation(text="Evidence not stored: " + ", ".join(missing), x=0, y=1.12,
                           xref="paper", yref="paper", showarrow=False)
    fig.update_layout(title=f"Signal {signal.get('id')} — {escape(str(signal.get('pair', '')))} "
                      + ("(recorded window)" if evidence.get("candles") else "(available historical context)"),
                      template="plotly_white", xaxis_rangeslider_visible=False, xaxis_title="UTC")
    return fig


def recent_news(now):
    # NOTE: get_upcoming_news excludes past events. Use the existing client's
    # read machinery for the complete ±15-minute gate so startup cannot miss
    # past events and a failed future getter cannot silently clear the gate.
    try:
        return db._all_rows(db._get_client().table("news_events").select("*").gte(
            "scheduled_at", (now-timedelta(minutes=15)).isoformat()).lte(
                "scheduled_at", (now+timedelta(minutes=15)).isoformat()).order("scheduled_at"))
    except Exception as exc:
        logger.error("Recent news read failed (%s)", type(exc).__name__)
        return None


def _panel(fn, fallback):
    try:
        return fn()
    except Exception as exc:
        logger.error("Dashboard panel failed (%s)", type(exc).__name__)
        return fallback()


@lru_cache(maxsize=1)
def equity_history(minute):
    # NOTE: At a 3-second reporting cadence, a 30-day read every 10 seconds
    # overwhelms this instance. Cache 24 hours for one minute; latest equity
    # still refreshes every tick. Retain extrema per bucket in the chart.
    end = datetime.fromtimestamp(minute * 60, UTC)
    return db.get_equity_snapshots(end-timedelta(hours=24))


def refresh():
    now = datetime.now(UTC)
    snapshots = list(_panel(lambda: equity_history(int(now.timestamp())//60), list) or [])
    latest = _panel(db.get_latest_equity_snapshot, dict) or {}
    if latest and not any(r.get("ts") == latest.get("ts") for r in snapshots):
        snapshots.append(latest)
    trades = _panel(lambda: db.get_trades(100), list)
    positions = _panel(db.get_open_trades, list)
    events = _panel(lambda: db.get_upcoming_news(1440), list)
    signal = _panel(db.get_latest_signal, dict) or {}
    candles = _panel(lambda: db.get_latest_candles(signal["pair"], "M1", 500), list) if signal else []
    pairs = _panel(lambda: load_config().TRADED_PAIRS, lambda: [])
    age = (now-timestamp(latest["ts"])).total_seconds() if timestamp(latest.get("ts")) else None
    status = f"Updated {now.isoformat()} — equity snapshot age: {age:.0f}s" if age is not None else "No data yet — equity snapshot unavailable"
    if age is not None and age > 30:
        status += " — STALE: displayed values may not reflect current risk"
    return (status,
        _panel(lambda: build_equity_figure(snapshots), lambda: empty_figure("Equity curve")),
        _panel(lambda: build_drawdown_gauge(latest.get("daily_dd_pct")), lambda: empty_figure("Daily drawdown")),
        _panel(lambda: build_drawdown_gauge(latest.get("overall_dd_pct"), False), lambda: empty_figure("Overall drawdown")),
        killzone_text(now), _panel(lambda: trade_rows(trades), lambda: trade_rows([])),
        _panel(lambda: position_rows(positions), lambda: position_rows([])),
        _panel(lambda: news_text(events, recent_news(now), pairs, now), lambda: "No data yet — blackout unknown"),
        _panel(lambda: build_setup_figure(signal, candles), lambda: empty_figure("Latest setup")))


def create_dashboard():
    import gradio as gr
    with gr.Blocks(title="ICT Silver Bullet", analytics_enabled=False) as app:
        gr.Markdown("# ICT Silver Bullet\nRead-only monitoring · timestamps in UTC")
        status = gr.Markdown("No data yet")
        equity = gr.Plot(label="Equity")
        with gr.Row():
            daily = gr.Plot(label="Daily drawdown")
            overall = gr.Plot(label="Overall drawdown")
        clock = gr.Markdown("No data yet")
        trades = gr.Dataframe(headers=TRADE_HEADERS, datatype=["str"]*len(TRADE_HEADERS), label="Trade log", interactive=False)
        positions = gr.Dataframe(headers=POSITION_HEADERS, datatype=["str"]*len(POSITION_HEADERS), label="Open positions", interactive=False)
        news = gr.Markdown("No data yet")
        setup = gr.Plot(label="Setup audit")
        outputs = [status, equity, daily, overall, clock, trades, positions, news, setup]
        app.load(refresh, outputs=outputs)
        gr.Timer(10).tick(refresh, outputs=outputs)
    return app


def main() -> None:
    setup_logging("brain")
    config = load_dashboard_config()  # Refuse to bind without both credentials.
    app = create_dashboard()
    # If later using Tailscale (free personal plan), set DASHBOARD_HOST to
    # 127.0.0.1 or the tailnet IP instead of exposing port 7860.
    try:
        app.launch(server_name=config.DASHBOARD_HOST, server_port=config.DASHBOARD_PORT,
                   auth=(config.DASHBOARD_USERNAME, config.DASHBOARD_PASSWORD), share=False)
    except KeyboardInterrupt:
        logger.info("Dashboard stopped")
    finally:
        app.close()


if __name__ == "__main__":
    main()
