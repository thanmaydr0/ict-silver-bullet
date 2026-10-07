from types import SimpleNamespace
from unittest.mock import Mock
import sys
import pytest
from brain.news import sentiment_local as sl

HEADLINE = {"title": "USD CPI and Federal Reserve rates", "source": "wire"}
LABELS = [{"label": "positive", "score": .8}, {"label": "negative", "score": .1}, {"label": "neutral", "score": .1}]


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    monkeypatch.setattr(sl, "_cache", {})
    monkeypatch.setattr(sl, "_model", None)
    monkeypatch.setattr(sl, "_model_failed", False)
    monkeypatch.setattr(sl, "load_config", lambda: SimpleNamespace(SENTIMENT_MODEL="ProsusAI/finbert"))
    # Fake modules prevent even an accidental real model import/download.
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(set_num_threads=Mock()))
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(pipeline=Mock(side_effect=RuntimeError("mock model unavailable"))))


def test_batch_cache_index_and_pair_relevance(monkeypatch):
    model = Mock(return_value=[LABELS, [{"label": "negative", "score": .7}]])
    monkeypatch.setattr(sl, "_load_model", lambda: model)
    other = {"title": "Yen tumbles", "source": "wire"}
    results = sl.score_headlines_batch([HEADLINE, other, HEADLINE], "EURUSD")
    assert results == [{"relevance": 1.0, "direction": "bullish", "confidence": .8},
                       {"relevance": .2, "direction": "bearish", "confidence": .7},
                       {"relevance": 1.0, "direction": "bullish", "confidence": .8}]
    model.assert_called_once_with([HEADLINE["title"], other["title"]], batch_size=16, truncation=True)
    assert sl.score_headlines_batch([other], "USDJPY")[0]["relevance"] == pytest.approx(.6)
    assert sl.score_headlines_batch([HEADLINE])[0]["relevance"] == pytest.approx(.6)
    assert model.call_count == 1


@pytest.mark.parametrize("labels", [[], [{"label": "unknown", "score": .9}],
    [{"label": "positive", "score": 1.1}], [{"label": "positive", "score": float("nan")}],
    [{"label": "positive", "score": "0.8"}], [{"label": "positive", "score": .8}, {"label": "negative", "score": -1}], {}])
def test_output_validation(monkeypatch, labels):
    monkeypatch.setattr(sl, "_load_model", lambda: Mock(return_value=[labels]))
    assert sl.score_headlines_batch([HEADLINE]) == [sl._DEFAULT]


def test_malformed_item_does_not_shift_index(monkeypatch):
    monkeypatch.setattr(sl, "_load_model", lambda: Mock(return_value=[LABELS]))
    result = sl.score_headlines_batch([{}, HEADLINE, {"title": None}, None])
    assert result[0] == result[2] == result[3] == sl._DEFAULT
    assert result[1]["direction"] == "bullish"


def test_model_load_failure_latched():
    assert sl.score_headlines_batch([HEADLINE, HEADLINE]) == [sl._DEFAULT, sl._DEFAULT]
    assert sl.score_headlines_batch([HEADLINE]) == [sl._DEFAULT]
    assert sl._model_failed
    sys.modules["transformers"].pipeline.assert_called_once()


def test_lazy_loader_once_and_cpu():
    model = Mock(return_value=[LABELS])
    factory = sys.modules["transformers"].pipeline
    factory.side_effect = None
    factory.return_value = model
    assert sl._load_model() is model
    assert sl._load_model() is model
    factory.assert_called_once_with("text-classification", model="ProsusAI/finbert", top_k=None, device=-1)
    sys.modules["torch"].set_num_threads.assert_called_once_with(1)


@pytest.mark.parametrize("output", [[], None, [LABELS, LABELS]])
def test_batch_length_failure(monkeypatch, output):
    monkeypatch.setattr(sl, "_load_model", lambda: Mock(return_value=output))
    assert sl.score_headlines_batch([HEADLINE]) == [sl._DEFAULT]


def test_empty_batch_does_not_load(monkeypatch):
    loader = Mock(side_effect=AssertionError("should not load"))
    monkeypatch.setattr(sl, "_load_model", loader)
    assert sl.score_headlines_batch([]) == []
    loader.assert_not_called()


def test_hash_includes_source(monkeypatch):
    model = Mock(return_value=[LABELS, LABELS])
    monkeypatch.setattr(sl, "_load_model", lambda: model)
    sl.score_headlines_batch([HEADLINE, {**HEADLINE, "source": "other"}])
    assert len(sl._cache) == 2
