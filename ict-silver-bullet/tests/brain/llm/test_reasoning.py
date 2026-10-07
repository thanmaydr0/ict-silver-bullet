"""LLM judgment tests: provider calls and configuration are fully mocked."""

import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

from brain.llm import reasoning
from brain.llm.prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from brain.llm.schema import LLMVerdict


VALID = {
    "verdict": "approve",
    "conviction": 0.85,
    "reasoning": "Sweep and displacement support the higher-timeframe bias.",
    "key_risk": "Upcoming news may weaken the narrative.",
}


@pytest.fixture
def context():
    return {
        "pair": "EURUSD",
        "htf_bias": "bullish",
        "killzone_name": "London",
        "sweep_description": "Sell-side liquidity swept below the prior low",
        "displacement_pips": 12,
        "displacement_atr_ratio": 1.8,
        "fvg_top": 1.1020,
        "fvg_bottom": 1.1010,
        "ote_overlap_bool": True,
        "mechanical_score": 8,
        "candles_summary": "Last 20 candles: sweep followed by bullish expansion",
        "recent_headlines": "Euro firms ahead of employment data",
        "next_event_name": "US employment report",
        "next_event_minutes_until": 90,
        "realized_r_today": -0.5,
        "daily_dd_used": 0.5,
        "daily_dd_limit": 3.0,
        "trades_taken_today": 1,
    }


@pytest.fixture(params=["anthropic", "openai"])
def provider(request, monkeypatch):
    settings = SimpleNamespace(
        LLM_PROVIDER=request.param, LLM_API_KEY="dummy-test-key", LLM_MODEL="test-model"
    )
    monkeypatch.setattr(reasoning, "load_config", Mock(return_value=settings))
    api = Mock()
    monkeypatch.setattr(reasoning, f"_call_{request.param}", api)
    return api


def assert_fail_safe(result):
    assert result == LLMVerdict(
        verdict="downweight",
        conviction=0.0,
        reasoning="LLM call failed or returned invalid output; failing safe.",
        key_risk="unknown - LLM unavailable",
    )


def test_valid_json_parses(context, provider):
    provider.return_value = json.dumps(VALID)
    result = reasoning.review_setup(context)
    assert isinstance(result, LLMVerdict)
    assert result.model_dump() == VALID
    provider.assert_called_once_with(
        "dummy-test-key", "test-model", USER_PROMPT_TEMPLATE.format(**context)
    )


def test_malformed_json_retries_once_and_succeeds(context, provider):
    provider.side_effect = ["{bad json", json.dumps(VALID)]
    assert reasoning.review_setup(context).model_dump() == VALID
    assert provider.call_count == 2
    original = provider.call_args_list[0].args[2]
    correction = provider.call_args_list[1].args[2]
    assert correction.startswith(original)
    assert "Invalid JSON" in correction
    assert "Correct the JSON" in correction


def test_twice_malformed_fails_safe(context, provider):
    provider.side_effect = ["{bad json", "still not json"]
    assert_fail_safe(reasoning.review_setup(context))
    assert provider.call_count == 2


def test_timeout_fails_safe_without_retry(context, provider):
    provider.side_effect = TimeoutError("simulated timeout")
    assert_fail_safe(reasoning.review_setup(context))
    assert provider.call_count == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"conviction": -0.01},
        {"conviction": 1.01},
        {"conviction": float("nan")},
        {"conviction": "0.85"},
        {"verdict": "buy"},
        {"reasoning": None},
        {"unexpected": "extra field"},
    ],
)
def test_schema_invalid_output_retries_then_fails_safe(context, provider, changes):
    provider.return_value = json.dumps({**VALID, **changes})
    assert_fail_safe(reasoning.review_setup(context))
    assert provider.call_count == 2


def test_schema_error_text_is_in_correction(context, provider):
    provider.side_effect = [json.dumps({**VALID, "conviction": 2}), json.dumps(VALID)]
    assert reasoning.review_setup(context).verdict == "approve"
    assert "less than or equal to 1" in provider.call_args_list[1].args[2]


def test_timeout_on_correction_fails_safe(context, provider):
    provider.side_effect = ["bad json", TimeoutError("simulated timeout")]
    assert_fail_safe(reasoning.review_setup(context))
    assert provider.call_count == 2


def test_missing_context_fails_safe_without_api_call(context, provider):
    del context["pair"]
    assert_fail_safe(reasoning.review_setup(context))
    provider.assert_not_called()


def test_unavailable_config_fails_safe(context, provider, monkeypatch):
    monkeypatch.setattr(reasoning, "load_config", Mock(side_effect=RuntimeError("missing config")))
    assert_fail_safe(reasoning.review_setup(context))
    provider.assert_not_called()


def test_unknown_provider_fails_safe(context, monkeypatch):
    monkeypatch.setattr(
        reasoning, "load_config", Mock(return_value=SimpleNamespace(LLM_PROVIDER="unknown"))
    )
    anthropic = Mock()
    openai = Mock()
    monkeypatch.setattr(reasoning, "_call_anthropic", anthropic)
    monkeypatch.setattr(reasoning, "_call_openai", openai)
    assert_fail_safe(reasoning.review_setup(context))
    anthropic.assert_not_called()
    openai.assert_not_called()


@pytest.mark.parametrize("provider_name", ["anthropic", "openai"])
def test_provider_adapters_send_system_and_user_prompts(provider_name, monkeypatch):
    client = MagicMock()
    factory = Mock()
    factory.return_value.__enter__ = Mock(return_value=client)
    factory.return_value.__exit__ = Mock(return_value=False)
    if provider_name == "anthropic":
        monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace(Anthropic=factory))
        client.messages.create.return_value = SimpleNamespace(content=[
            SimpleNamespace(type="text", text=json.dumps(VALID))
        ])
        create = client.messages.create
    else:
        monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=factory))
        client.chat.completions.create.return_value = SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(VALID)))
        ])
        create = client.chat.completions.create

    output = getattr(reasoning, f"_call_{provider_name}")("dummy-test-key", "test-model", "setup")
    assert json.loads(output) == VALID
    factory.assert_called_once_with(api_key="dummy-test-key", timeout=30.0, max_retries=0)
    assert create.call_count == 1
    arguments = create.call_args.kwargs
    assert arguments["model"] == "test-model"
    assert arguments["messages"][-1] == {"role": "user", "content": "setup"}
    if provider_name == "anthropic":
        assert arguments["system"] == SYSTEM_PROMPT
        assert arguments["max_tokens"] == 1024
    else:
        assert arguments["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}


def test_prompt_includes_all_context_and_risk_role(context):
    prompt = USER_PROMPT_TEMPLATE.format(**context)
    for value in context.values():
        assert str(value) in prompt
    assert "NOT the risk decision-maker" in SYSTEM_PROMPT
    assert "Return ONLY one JSON object" in SYSTEM_PROMPT
