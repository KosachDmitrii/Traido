"""LLM factory — Anthropic when keyed, else None (agents use heuristics)."""

from __future__ import annotations

from core.config import Settings
from core.llm.anthropic import AnthropicLLM
from core.ports import LLMPort


def create_llm(settings: Settings) -> LLMPort | None:
    if settings.anthropic_api_key:
        return AnthropicLLM(settings.anthropic_api_key)
    return None
