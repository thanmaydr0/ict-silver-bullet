"""Equity breakers buffered below actual 4% daily / 6% overall account limits.

The buffers deliberately absorb slippage and gap risk on the triggering trade.
All percentages are fractions, e.g. 0.025 means 2.5%.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from math import isfinite
from zoneinfo import ZoneInfo

SOFT_DAILY_DD_PCT = 0.025
HARD_DAILY_DD_PCT = 0.0325
SOFT_OVERALL_DD_PCT = 0.045
HARD_OVERALL_DD_PCT = 0.0525
NEW_YORK = ZoneInfo("America/New_York")
TRADING_DAY_ROLLOVER = time(17, 0)
# NOTE: User must confirm 17:00 America/New_York matches the account's daily DD reset.


def trading_day_start(now: datetime) -> datetime:
    """Return the current FX trading day's 17:00 NY rollover in UTC."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(NEW_YORK)
    day = local.date() if local.time() >= TRADING_DAY_ROLLOVER else local.date() - timedelta(days=1)
    return datetime.combine(day, TRADING_DAY_ROLLOVER, NEW_YORK).astimezone(timezone.utc)


@dataclass
class DrawdownState:
    """Track floating equity, a day-start balance, and account-lifetime peak equity.

    Restore high_water_mark_equity and day_start from persisted state on restart.
    At rollover update() uses the supplied current balance as day-start balance.
    Callers must supply a snapshot at rollover for an exact historical baseline.
    """

    day_start_balance: float
    current_equity: float
    high_water_mark_equity: float | None = None
    day_start: datetime | None = None

    def __post_init__(self):
        if self.high_water_mark_equity is None:
            self.high_water_mark_equity = self.current_equity
        self._validate(self.day_start_balance, self.current_equity, self.high_water_mark_equity)
        self.high_water_mark_equity = max(self.high_water_mark_equity, self.current_equity)
        if self.day_start is not None:
            self.day_start = trading_day_start(self.day_start)

    @staticmethod
    def _validate(balance, equity, peak):
        if not all(isfinite(v) for v in (balance, equity, peak)) or balance <= 0 or peak <= 0:
            raise ValueError("balance and peak must be positive; equity must be finite")

    @property
    def daily_dd_pct(self) -> float:
        return (self.day_start_balance - self.current_equity) / self.day_start_balance

    @property
    def overall_dd_pct(self) -> float:
        return (self.high_water_mark_equity - self.current_equity) / self.high_water_mark_equity

    def update(self, current_equity: float, current_balance: float, now: datetime) -> None:
        """Update equity and peak, resetting only the daily baseline at rollover."""
        start = trading_day_start(now)
        self._validate(current_balance, current_equity, self.high_water_mark_equity)
        if self.day_start is not None and start < self.day_start:
            raise ValueError("equity snapshots must not precede the current trading day")
        if self.day_start != start:
            self.day_start_balance = current_balance
            self.day_start = start
        self.current_equity = current_equity
        self.high_water_mark_equity = max(self.high_water_mark_equity, current_equity)


def check_breakers(daily_dd_pct: float, overall_dd_pct: float) -> dict:
    """Return every active breaker mapped to its action, including soft + hard."""
    if not all(isfinite(v) for v in (daily_dd_pct, overall_dd_pct)):
        raise ValueError("drawdowns must be finite fractions")
    rules = (
        ("soft_daily", daily_dd_pct, SOFT_DAILY_DD_PCT, "reduce_size_50pct"),
        ("hard_daily", daily_dd_pct, HARD_DAILY_DD_PCT, "close_all_disable_new"),
        ("soft_overall", overall_dd_pct, SOFT_OVERALL_DD_PCT, "reduce_size_50pct_and_raise_min_score_to_9"),
        ("hard_overall", overall_dd_pct, HARD_OVERALL_DD_PCT, "halt_require_manual_review"),
    )
    return {name: action for name, value, threshold, action in rules if value >= threshold}
