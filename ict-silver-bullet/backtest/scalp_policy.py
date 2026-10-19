"""Pure offline execution/risk policy; production processes do not import it.

OHLC and latency allowances are conditional models, never a no-breach guarantee.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from functools import lru_cache
from zoneinfo import ZoneInfo

NY = ZoneInfo('America/New_York')
UTC = timezone.utc


def aware(value):
    value = datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value
    if value.utcoffset() is None:
        raise ValueError('timezone-aware timestamps required')
    return value.astimezone(UTC)


@lru_cache(maxsize=65536)
def fx_day(now):
    local = aware(now).astimezone(NY)
    return (local - timedelta(hours=17)).date()


@lru_cache(maxsize=65536)
def market_open(now):
    local = aware(now).astimezone(NY)
    minute = local.hour * 60 + local.minute
    return local.weekday() < 5 and 60 <= minute < 16 * 60 + 50


@dataclass(frozen=True)
class Costs:
    commission_round_trip: float = 7.0  # USD per standard lot
    slippage_pips: float = .15          # each fill
    latency_pips: float = .15           # additional adverse allowance at entry
    latency_seconds: float = 3.0        # allowance, not observable sub-minute fill
    multiplier: float = 1.0

    def __post_init__(self):
        values = (self.commission_round_trip, self.slippage_pips, self.latency_pips,
                  self.latency_seconds, self.multiplier)
        if not all(isfinite(x) and x >= 0 for x in values) or self.multiplier < 1:
            raise ValueError('finite nonnegative costs; multiplier >=1 required')
        if not 1 <= self.latency_seconds <= 3:
            raise ValueError('retail latency must be between 1 and 3 seconds')

    @lru_cache(maxsize=65536)
    def spread(self, symbol, now, recorded=None, news=False):
        if symbol not in ('EURUSD', 'GBPUSD'):
            raise ValueError('this study supports only the original USD-quoted pairs')
        if recorded is not None:
            if not isfinite(recorded) or recorded < 0:
                raise ValueError('recorded spread must be nonnegative pips')
            return recorded * self.multiplier
        base = {'EURUSD': 1., 'GBPUSD': 1.5}[symbol]
        hour = aware(now).astimezone(NY).hour
        factor = 4 if 16 <= hour < 18 else 1.5 if hour >= 18 or hour < 3 else 1
        return base * factor * (2 if news else 1) * self.multiplier

    def slip(self, entry=False):
        return (self.slippage_pips + (self.latency_pips if entry else 0)) * self.multiplier

    @property
    def commission(self):
        return self.commission_round_trip * self.multiplier


@dataclass
class Limits:
    """Floating-equity state, 17:00 NY balance reset, latched daily soft halt.

    The overall soft limit halves size; the daily soft limit flattens/halts.
    Hard overall is latched across days. All thresholds are fractions.
    """
    balance: float = 10000.
    peak: float = 10000.
    day_balance: float = 10000.
    day: object = None
    daily_halt: bool = False
    overall_halt: bool = False
    soft_daily: float = .02
    soft_overall: float = .05
    hard_daily: float = .03
    hard_overall: float = .07

    def new_day(self, now):
        day = fx_day(now)
        if self.day is not None and day < self.day:
            raise ValueError('risk clock cannot move backwards')
        if self.day != day:
            self.day = day
            self.day_balance = self.balance
            self.daily_halt = False

    def mark(self, equity, now):
        if not isfinite(equity):
            raise ValueError('finite equity required')
        self.new_day(now)
        self.peak = max(self.peak, equity)
        daily = max(0., 1-equity/self.day_balance)
        overall = max(0., 1-equity/self.peak)
        self.daily_halt |= daily >= self.soft_daily - 1e-12
        self.overall_halt |= overall >= self.hard_overall - 1e-12
        return dict(daily_dd=daily, overall_dd=overall,
                    soft_daily=daily >= self.soft_daily - 1e-12,
                    soft_overall=overall >= self.soft_overall - 1e-12,
                    breach_daily=daily > self.hard_daily + 1e-12,
                    breach_overall=overall > self.hard_overall + 1e-12,
                    hard_daily=daily >= self.hard_daily - 1e-12,
                    hard_overall=overall >= self.hard_overall - 1e-12)

    def budget(self, fraction, equity):
        if not isfinite(fraction) or not 0 < fraction <= .005:
            raise ValueError('risk must be in (0, .005]')
        if self.daily_halt or self.overall_halt or equity <= 0:
            return 0.
        reduced = .5 if 1-equity/self.peak >= self.soft_overall else 1.
        # NOTE: Reserve 10% of remaining daily-soft / overall-hard headroom.
        # Gaps and unobserved ticks can still overshoot; count actual breaches.
        headroom = min(equity-self.day_balance*(1-self.soft_daily),
                       equity-self.peak*(1-self.hard_overall))
        return max(0., min(equity*fraction*reduced, .9*headroom))
