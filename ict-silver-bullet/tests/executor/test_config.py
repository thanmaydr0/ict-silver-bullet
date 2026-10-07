"""Executor configuration tests without importing or installing MT5."""

import pytest

from executor import config


REQUIRED = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER", "WATCHDOG_WEBHOOK_URL")


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    config.load_config.cache_clear()
    monkeypatch.setattr(config, "_ENV_PATH", tmp_path / "settings.txt")
    for key in REQUIRED + ("TRADED_PAIRS", "BROKER_UTC_OFFSET_HOURS", "MT5_TERMINAL_PATH", "HEALTHCHECK_PING_URL"):
        monkeypatch.delenv(key, raising=False)
    yield
    config.load_config.cache_clear()


def populate(monkeypatch):
    for key in REQUIRED:
        monkeypatch.setenv(key, "42" if key == "MT5_LOGIN" else "test-value")


@pytest.mark.parametrize("missing", REQUIRED)
def test_required_variables_are_named(monkeypatch, missing):
    populate(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        config.load_config()


def test_defaults_isolation_and_cache(monkeypatch):
    populate(monkeypatch)
    settings = config.load_config()
    assert settings.MT5_LOGIN == 42
    assert settings.TRADED_PAIRS == ["EURUSD", "GBPUSD"]
    assert settings.BROKER_UTC_OFFSET_HOURS == 0
    assert settings.MT5_TERMINAL_PATH is None
    assert settings.HEALTHCHECK_PING_URL is None
    assert not hasattr(settings, "LLM_API_KEY")
    assert config.load_config() is settings


@pytest.mark.parametrize("login", ["invalid", "0", "-1"])
def test_login_validation(monkeypatch, login):
    populate(monkeypatch)
    monkeypatch.setenv("MT5_LOGIN", login)
    with pytest.raises(RuntimeError, match="MT5_LOGIN"):
        config.load_config()


def test_package_file_and_offset(monkeypatch):
    config._ENV_PATH.write_text("\n".join(f"{key}={'42' if key == 'MT5_LOGIN' else 'file-value'}" for key in REQUIRED), encoding="utf-8")
    monkeypatch.setenv("MT5_SERVER", "environment-value")
    monkeypatch.setenv("BROKER_UTC_OFFSET_HOURS", "-3")
    settings = config.load_config()
    assert settings.MT5_SERVER == "environment-value"
    assert settings.SUPABASE_URL == "file-value"
    assert settings.BROKER_UTC_OFFSET_HOURS == -3


def test_invalid_offset(monkeypatch):
    populate(monkeypatch)
    monkeypatch.setenv("BROKER_UTC_OFFSET_HOURS", "invalid")
    with pytest.raises(RuntimeError, match="BROKER_UTC_OFFSET_HOURS"):
        config.load_config()
