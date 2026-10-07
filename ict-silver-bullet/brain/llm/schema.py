"""Validated second-opinion output from the LLM judgment layer."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LLMVerdict(BaseModel):
    """A setup-quality opinion, never an authorization to place a trade."""

    model_config = ConfigDict(extra="forbid", strict=True)

    verdict: Literal["approve", "downweight", "veto"]
    conviction: float = Field(ge=0.0, le=1.0)
    reasoning: str
    key_risk: str
