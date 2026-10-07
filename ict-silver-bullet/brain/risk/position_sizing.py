"""Size positions within the tight 4%/6% drawdown budget."""

from decimal import Decimal, InvalidOperation, ROUND_FLOOR

DEFAULT_RISK_PCT = 0.0025


def calculate_lot_size(account_equity, risk_pct=DEFAULT_RISK_PCT,
                       stop_loss_pips=None, pip_value_per_lot=None, lot_step=0.01) -> float:
    """Risk equity * risk_pct, then floor to the broker step (never round up).

    risk_pct is configurable as a fraction; recommended range is 0.0025–0.005
    (0.25%–0.5%), given the account's tight DD budget.
    """
    try:
        equity, risk, stop, pip, step = map(lambda v: Decimal(str(v)),
                                          (account_equity, risk_pct, stop_loss_pips, pip_value_per_lot, lot_step))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("sizing inputs must be finite numbers") from exc
    if not all(v.is_finite() for v in (equity, risk, stop, pip, step)):
        raise ValueError("sizing inputs must be finite numbers")
    if equity <= 0 or stop <= 0 or pip <= 0 or step <= 0 or not 0 <= risk <= 1:
        raise ValueError("equity, stop, pip value and step must be positive; risk must be in [0, 1]")
    lots = equity * risk / (stop * pip)
    return float((lots / step).to_integral_value(rounding=ROUND_FLOOR) * step)
