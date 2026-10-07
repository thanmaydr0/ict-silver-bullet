"""USD-account pip estimates for standard 100,000-unit lots."""

# NOTE: These are approximations; exact values can later be read from MT5's
# symbol_info on the executor side. USDJPY pip value is 1000 / current price.
PAIR_SPECS = {
    "EURUSD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0},
    "GBPUSD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0},
    "AUDUSD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0},
    "NZDUSD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0},
    "USDJPY": {"pip_size": 0.01, "pip_value_per_lot": lambda price: 1000.0 / price},
}


def pair_spec(pair: str, price: float) -> dict:
    """Resolve quote-dependent values; unsupported pairs fail closed."""
    spec = PAIR_SPECS[pair]
    value = spec["pip_value_per_lot"]
    return {"pip_size": spec["pip_size"],
            "pip_value_per_lot": value(price) if callable(value) else value}
