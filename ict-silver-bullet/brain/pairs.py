"""USD-account pip estimates for standard 100,000-unit lots."""

# NOTE: These are USD-account estimates for 100,000-unit FX lots. Exact sizing
# continues to use the executor's broker metadata. USD-quoted pip values are
# fixed; USD-base values convert the quote-currency pip value back to USD.
PAIR_SPECS = {
    "EURUSD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0},
    "GBPUSD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0},
    "AUDUSD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0},
    "NZDUSD": {"pip_size": 0.0001, "pip_value_per_lot": 10.0},
    "USDJPY": {"pip_size": 0.01, "pip_value_per_lot": lambda price: 1000.0 / price},
    "USDCAD": {"pip_size": 0.0001, "pip_value_per_lot": lambda price: 10.0 / price},
    "USDCHF": {"pip_size": 0.0001, "pip_value_per_lot": lambda price: 10.0 / price},
}


def pair_spec(pair: str, price: float) -> dict:
    """Resolve quote-dependent values; unsupported pairs fail closed."""
    spec = PAIR_SPECS[pair]
    value = spec["pip_value_per_lot"]
    return {"pip_size": spec["pip_size"],
            "pip_value_per_lot": value(price) if callable(value) else value}
