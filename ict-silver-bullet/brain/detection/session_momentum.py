"""Causal FX adaptation of intraday noise-area momentum (research candidate).

Reference: Zarattini, Aziz and Barbon, 2024, SPY intraday momentum. That equity
study does not validate this FX adaptation. We use session TWAP, not exchange
VWAP, because our FX candles contain no reliable centralized traded volume.
"""

from collections import deque
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

STRATEGY = "session_momentum"
LOOKBACK = 14
SESSIONS = {"momentum_london": (ZoneInfo("Europe/London"), time(8), time(12)),
            "momentum_ny": (ZoneInfo("America/New_York"), time(8), time(12))}


def session_at(now):
    """Named weekday sessions with local DST, returned on a UTC clock."""
    if now.utcoffset() is None:
        raise ValueError("aware UTC-compatible time required")
    for name, (zone, opening, closing) in SESSIONS.items():
        local = now.astimezone(zone)
        if local.weekday() < 5 and opening <= local.time() < closing:
            return name
    return None


def bounds(name, now):
    zone, opening, closing = SESSIONS[name]
    day = now.astimezone(zone).date()
    return tuple(datetime.combine(day, t, zone).astimezone(timezone.utc) for t in (opening, closing))


class SessionHistory:
    """Observe completed M1 bars once; only complete prior sessions train bands.

    snapshot() returns plain data for the pure decision function. Neither a
    current partial session nor a future candle can update its training sample.
    Instances belong to the data adapter, never to evaluate_candles itself.
    """

    def __init__(self):
        self.past = {name: deque(maxlen=LOOKBACK) for name in SESSIONS}
        self.active = {}
        self.last = None
        self.available = None

    def observe(self, row):
        ts = row["ts"]
        if ts.utcoffset() is None:
            raise ValueError("aware candles required")
        if self.last is not None and ts <= self.last:
            raise ValueError("candles must be strictly chronological and distinct")
        self.last = ts
        self.available = ts + timedelta(minutes=1)
        for name in SESSIONS:
            opening, closing = bounds(name, ts)
            if not opening <= ts < closing or opening.weekday() >= 5:
                continue
            if name not in self.active or self.active[name]["opening"] != opening:
                self.active[name] = dict(opening=opening, close_at=closing, open=row["open"],
                    count=0, valid=ts == opening, total=0., moves={}, last=None)
            state = self.active[name]
            if state["last"] is not None and ts-state["last"] != timedelta(minutes=1):
                state["valid"] = False
            state["last"] = ts
            state["count"] += 1
            state["total"] += (row["high"]+row["low"]+row["close"])/3
            state["price"] = row["close"]
            elapsed = int((self.available-opening).total_seconds()/60)
            if elapsed % 30 == 0:
                state["moves"][elapsed] = abs(row["close"]/state["open"]-1)
            if self.available == closing and state["valid"] and state["count"] == 240:
                self.past[name].append(dict(opening=opening, moves=dict(state["moves"])))

    def snapshot(self, now):
        name = session_at(now)
        if not name or name not in self.active or self.available is None:
            return None
        state = self.active[name]
        opening, closing = bounds(name, now)
        if (state["opening"] != opening or not state["valid"] or
                not self.available <= now < self.available+timedelta(minutes=1)):
            return None
        elapsed = int((self.available-opening).total_seconds()/60)
        training = [p for p in self.past[name] if p["opening"] < opening and elapsed in p["moves"]]
        if elapsed < 30 or elapsed % 30 or self.available >= closing or len(training) < LOOKBACK:
            return None
        sigma = sum(p["moves"][elapsed] for p in training[-LOOKBACK:])/LOOKBACK
        if sigma <= 0:
            return None
        return dict(strategy=STRATEGY, session=name, opening=opening, close_at=closing,
            available_at=self.available, entry=state["price"], session_open=state["open"],
            upper=state["open"]*(1+sigma), lower=state["open"]*(1-sigma),
            twap=state["total"]/state["count"], sigma=sigma,
            training_moves=[p["moves"][elapsed] for p in training[-LOOKBACK:]],
            training_sessions=[p["opening"] for p in training[-LOOKBACK:]])


def detect(candles, context):
    """Create a market-entry candidate from a frozen, past-only session profile."""
    profile = context.get("session_profile")
    now = context["now"]
    if not profile or not candles or profile.get("strategy") != STRATEGY:
        return None
    opening, closing = bounds(profile["session"], now)
    training = profile["training_sessions"]
    if (session_at(now) != profile["session"] or profile["opening"] != opening or
            not profile["available_at"] <= now < profile["available_at"]+timedelta(minutes=1) or
            len(training) != LOOKBACK or len(set(training)) != LOOKBACK or
            any(day >= opening for day in training) or closing-profile["available_at"] < timedelta(minutes=30) or
            candles[-1]["ts"]+timedelta(minutes=1) != profile["available_at"]):
        return None
    entry = candles[-1]["close"]
    if entry != profile["entry"]:
        return None
    buy = entry > max(profile["upper"], profile["twap"])
    sell = entry < min(profile["lower"], profile["twap"])
    if not buy and not sell:
        return None
    stop = profile["lower"] if buy else profile["upper"]
    # NOTE: Hard protection at the opposite band bounds pre-exit account risk.
    # No fixed target: at half-hour checkpoints exit on current-band/TWAP loss,
    # or at session close. These FX execution choices differ from the SPY paper.
    return dict(strategy=STRATEGY, pair=context["pair"], direction="buy" if buy else "sell",
        entry=entry, stop_loss=stop, take_profit=None, setup_id=profile["available_at"].isoformat(),
        htf_bias="intraday_up" if buy else "intraday_down", atr=profile["upper"]-profile["lower"],
        sweep=dict(description="not used; session noise-band breakout"),
        confirmation=dict(displacement=abs(entry-profile["session_open"])),
        evidence=dict(profile), factors=dict(strategy=STRATEGY, inside_killzone=True,
            past_only_noise_profile=True, outside_noise_band=True, twap_aligned=True,
            protected_session_trade=True))


def exit_reason(position, profile, now):
    """Shared pure exit policy; broker hard-stop handling is separate."""
    if now >= position["close_at"]:
        return "session_close"
    if not profile or profile["session"] != position["session"]:
        return None
    if not profile["available_at"] <= now < profile["available_at"]+timedelta(minutes=1):
        return None
    if profile["available_at"] <= position["opened_at"]:
        return None
    if position["direction"] == "buy":
        return "momentum_lost" if profile["entry"] <= max(profile["upper"], profile["twap"]) else None
    return "momentum_lost" if profile["entry"] >= min(profile["lower"], profile["twap"]) else None
