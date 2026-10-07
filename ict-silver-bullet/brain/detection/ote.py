"""Optimal trade entry retracement bands."""


def calculate_ote_zone(leg_start_price: float, leg_end_price: float) -> tuple[float, float]:
    """Return sorted 61.8%-79% retracement prices measured from the leg end."""
    leg = leg_end_price - leg_start_price
    prices = (leg_end_price - 0.618 * leg, leg_end_price - 0.79 * leg)
    return min(prices), max(prices)


def price_in_ote(price: float, ote_zone: tuple[float, float]) -> bool:
    """Include both boundaries of the (low, high) band."""
    low, high = ote_zone
    return low <= price <= high
