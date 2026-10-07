"""Small deterministic candles with external indices distinct from offsets."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

import pytest


@dataclass
class Candle:
    index: int
    open: float
    high: float
    low: float
    close: float
    ts: datetime


@pytest.fixture(params=[False, True], ids=["objects", "dicts"])
def candles(request):
    def build(rows, days=None):
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = [Candle(100 + i * 10, *row,
                         start + timedelta(days=days[i] if days else 0, minutes=i))
                  for i, row in enumerate(rows)]
        return [asdict(c) for c in result] if request.param else result
    return build
