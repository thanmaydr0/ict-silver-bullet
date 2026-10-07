"""Small CPU sentiment model. Direction is general market sentiment; the LLM
reasoning layer must map it to a specific currency pair's direction.
"""

import hashlib
import json
import re
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from brain.config import load_config
from brain.logging_setup import setup_logging
from brain.news.blackout import _currencies

logger = setup_logging("news_sentiment")
_model = None
_model_failed = False
_model_lock = Lock()
_score_lock = Lock()
_cache: dict[str, dict] = {}
_DEFAULT = {"relevance": 0.0, "direction": "neutral", "confidence": 0.0}


class HeadlineScore(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)
    relevance: float = Field(ge=0, le=1)
    direction: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0, le=1)


def _load_model():
    global _model, _model_failed
    with _model_lock:
        if _model is not None:
            return _model
        if _model_failed:
            return None
        try:
            # First run downloads from the public Hugging Face Hub; no account
            # or token is needed. Tests must mock this model entirely.
            import torch
            from transformers import pipeline
            torch.set_num_threads(1)
            name = load_config().SENTIMENT_MODEL or "ProsusAI/finbert"
            _model = pipeline("text-classification", model=name, top_k=None, device=-1)
            return _model
        except Exception:
            # NOTE: A load failure is latched until restart to avoid repeatedly
            # downloading/loading under memory pressure on the shared host.
            _model_failed = True
            logger.error("CPU sentiment model could not load; using neutral defaults")
            return None


_MACRO = (
    r"\b(?:central banks?|federal reserve|fed|ecb|bank of england|bank of japan|"
    r"bank of canada|reserve bank|snb|cpi|inflation|nfp|non[ -]?farm(?: payrolls)?|"
    r"interest rates?|rates?|gdp|gross domestic product)\b"
)
_NAMES = {
    "USD": ("us dollar", "u.s. dollar", "dollar", "federal reserve"),
    "EUR": ("euro", "eurozone", "ecb"),
    "GBP": ("pound", "sterling", "bank of england"),
    "JPY": ("yen", "bank of japan"),
    "CHF": ("swiss franc", "snb"),
    "CAD": ("canadian dollar", "bank of canada"),
    "AUD": ("australian dollar", "reserve bank of australia"),
    "NZD": ("new zealand dollar", "reserve bank of new zealand"),
}


def _relevance(title: str, pair: str | None) -> float:
    """Heuristic: .2 base, +.4 macro keyword, +.4 pair code/name; cap at 1.

    Matches whole words case-insensitively. Without a pair, only macro relevance
    applies. This is lexical relevance, not a model estimate of trade quality.
    """
    score = .2 + .4 * bool(re.search(_MACRO, title, re.I))
    if pair:
        terms = []
        for code in _currencies(pair):
            terms.extend((code, *_NAMES.get(code, ())))
        score += .4 * any(re.search(r"\b" + re.escape(term) + r"\b", title, re.I) for term in terms)
    return min(1.0, max(0.0, score))


def _validate(score):
    try:
        return HeadlineScore.model_validate(score).model_dump()
    except Exception:
        return _DEFAULT.copy()


def score_headlines_batch(headlines: list[dict], pair: str | None = None) -> list[dict]:
    """Return one validated score per input index, with a single batch call.

    Cache stores model direction/confidence; relevance is recalculated per pair.
    Duplicate titles/sources within a batch are inferred only once.
    """
    if not headlines:
        return []
    with _score_lock:
        keys, pending = [], {}
        for headline in headlines:
            if (not isinstance(headline, dict) or not isinstance(headline.get("title"), str)
                    or not headline["title"].strip() or not isinstance(headline.get("source", ""), str)):
                keys.append(None)
                continue
            key = hashlib.sha256(json.dumps(
                [headline["title"], headline.get("source", "")], ensure_ascii=False).encode()).hexdigest()
            keys.append(key)
            if key not in _cache:
                pending.setdefault(key, headline["title"])
        if pending:
            try:
                model = _load_model()
                if model is None:
                    return [_DEFAULT.copy() for _ in headlines]
                output = model(list(pending.values()), batch_size=16, truncation=True)
                if not isinstance(output, list) or len(output) != len(pending):
                    raise ValueError("Model batch length mismatch")
                for key, labels in zip(pending, output):
                    try:
                        if not isinstance(labels, list) or not labels:
                            raise ValueError("Missing labels")
                        # Validate all probabilities, including nonwinning labels.
                        validated = []
                        for label in labels:
                            direction = {"positive": "bullish", "negative": "bearish", "neutral": "neutral"}[label["label"].lower()]
                            validated.append(HeadlineScore(relevance=0.0, direction=direction, confidence=label["score"]).model_dump())
                        top = max(validated, key=lambda row: row["confidence"])
                        _cache[key] = top
                    except Exception:
                        _cache[key] = _DEFAULT.copy()
            except Exception:
                logger.error("CPU sentiment inference failed; using neutral defaults")
                return [_DEFAULT.copy() for _ in headlines]
        results = []
        for headline, key in zip(headlines, keys):
            try:
                if key is None or _cache[key] == _DEFAULT:
                    results.append(_DEFAULT.copy())
                    continue
                results.append(_validate({**_cache[key], "relevance": _relevance(headline["title"], pair)}))
            except Exception:
                results.append(_DEFAULT.copy())
        return results
