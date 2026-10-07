"""Provider-independent, fail-safe LLM setup review."""

import logging

from pydantic import ValidationError

from brain.config import load_config
from brain.llm.prompts import SYSTEM_PROMPT, USER_PROMPT_TEMPLATE
from brain.llm.schema import LLMVerdict


logger = logging.getLogger(__name__)
# NOTE: Bound each SDK request to 30 seconds and disable its automatic retries;
# only an invalid JSON/schema response gets the single correction attempt.
API_TIMEOUT_SECONDS = 30.0


def _call_anthropic(api_key: str, model: str, user_prompt: str) -> str:
    # NOTE: Lazy SDK imports keep importing the judgment layer credential-free
    # and allow tests to replace providers without installing their SDKs.
    from anthropic import Anthropic

    with Anthropic(
        api_key=api_key, timeout=API_TIMEOUT_SECONDS, max_retries=0
    ) as client:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
    return "".join(block.text for block in response.content if block.type == "text")


def _call_openai(api_key: str, model: str, user_prompt: str) -> str:
    from openai import OpenAI

    with OpenAI(
        api_key=api_key, timeout=API_TIMEOUT_SECONDS, max_retries=0
    ) as client:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
    return response.choices[0].message.content or ""


def _fail_safe() -> LLMVerdict:
    return LLMVerdict(
        verdict="downweight",
        conviction=0.0,
        reasoning="LLM call failed or returned invalid output; failing safe.",
        key_risk="unknown - LLM unavailable",
    )


def review_setup(context: dict) -> LLMVerdict:
    """Review context whose keys match USER_PROMPT_TEMPLATE placeholders.

    Callers supply a summary of the last 20 candles as ``candles_summary``.
    Missing context/configuration, unavailable providers, and API errors all
    fail safe. Invalid JSON or schema output gets exactly one correction call.
    """
    try:
        settings = load_config()
        provider = settings.LLM_PROVIDER.strip().lower()
        if provider == "anthropic":
            call_provider = _call_anthropic
        elif provider == "openai":
            call_provider = _call_openai
        else:
            raise ValueError("Unsupported LLM provider")

        user_prompt = USER_PROMPT_TEMPLATE.format(**context)
        for attempt in range(2):
            output = call_provider(settings.LLM_API_KEY, settings.LLM_MODEL, user_prompt)
            try:
                return LLMVerdict.model_validate_json(output)
            except ValidationError as error:
                if attempt == 1:
                    logger.warning("LLM output remained invalid after one correction")
                    return _fail_safe()
                user_prompt += (
                    "\n\nYour response failed validation:\n"
                    f"{error}\n"
                    "Correct the JSON to match the required schema. Return ONLY "
                    "the corrected JSON object, with no surrounding prose."
                )
    except Exception:
        # Avoid logging provider exception text, which can contain credentials
        # or sensitive request context.
        logger.warning("LLM review unavailable; failing safe")
    return _fail_safe()
