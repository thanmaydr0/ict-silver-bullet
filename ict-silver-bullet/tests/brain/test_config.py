"""Credential-free checks for deferred, independent brain settings."""

import pytest

from brain import config


REQUIRED = ("SUPABASE_URL", "SUPABASE_SERVICE_KEY", "LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "FIRECRAWL_API_KEY")
NEWS = ("FRED_API_KEY", "FINNHUB_API_KEY", "MARKETAUX_API_KEY", "ALPHAVANTAGE_API_KEY", "NEWSDATA_API_KEY")


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    config.load_config.cache_clear()
    config.load_dashboard_config.cache_clear()
    monkeypatch.setattr(config, "_ENV_PATH", tmp_path / "settings.txt")
    for key in REQUIRED + NEWS + ("TRADED_PAIRS", "SENTIMENT_MODEL", "DASHBOARD_HOST", "DASHBOARD_PORT", "DASHBOARD_USERNAME", "DASHBOARD_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    yield
    config.load_config.cache_clear()
    config.load_dashboard_config.cache_clear()


def populate(monkeypatch):
    for key in REQUIRED:
        monkeypatch.setenv(key, "test-value")


@pytest.mark.parametrize("missing", REQUIRED)
def test_required_variables_are_named(monkeypatch, missing):
    populate(monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(RuntimeError, match=missing):
        config.load_config()


def test_news_optional_defaults_and_cache(monkeypatch):
    populate(monkeypatch)
    settings = config.load_config()
    assert settings.TRADED_PAIRS == ["EURUSD", "GBPUSD"]
    assert settings.SENTIMENT_MODEL == "ProsusAI/finbert"
    assert all(getattr(settings, key) is None for key in NEWS)
    assert not hasattr(settings, "MT5_PASSWORD")
    monkeypatch.setenv("LLM_MODEL", "changed")
    assert config.load_config() is settings
    assert settings.LLM_MODEL == "test-value"


def test_package_file_and_environment_precedence(monkeypatch):
    config._ENV_PATH.write_text("\n".join(f"{key}=file-value" for key in REQUIRED), encoding="utf-8")
    monkeypatch.setenv("LLM_MODEL", "environment-value")
    monkeypatch.setenv("TRADED_PAIRS", " USDJPY, EURUSD , ")
    settings = config.load_config()
    assert settings.SUPABASE_URL == "file-value"
    assert settings.LLM_MODEL == "environment-value"
    assert settings.TRADED_PAIRS == ["USDJPY", "EURUSD"]


def test_dashboard_validates_independently(monkeypatch):
    with pytest.raises(RuntimeError, match="DASHBOARD_USERNAME"):
        config.load_dashboard_config()
    monkeypatch.setenv("DASHBOARD_USERNAME", "test-user")
    with pytest.raises(RuntimeError, match="DASHBOARD_PASSWORD"):
        config.load_dashboard_config()
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-password")
    settings = config.load_dashboard_config()
    assert (settings.DASHBOARD_HOST, settings.DASHBOARD_PORT) == ("0.0.0.0", 7860)
    assert config.load_dashboard_config() is settings


@pytest.mark.parametrize("port", ["invalid", "0", "65536"])
def test_dashboard_port_error_names_variable(monkeypatch, port):
    monkeypatch.setenv("DASHBOARD_USERNAME", "test-user")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "test-password")
    monkeypatch.setenv("DASHBOARD_PORT", port)
    with pytest.raises(RuntimeError, match="DASHBOARD_PORT"):
        config.load_dashboard_config()
