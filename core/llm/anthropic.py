"""Anthropic Claude structured JSON adapter (optional)."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx


class AnthropicLLM:
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-20250514") -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = "https://api.anthropic.com/v1/messages"

    async def complete_json(
        self,
        *,
        system: str,
        user: str,
        schema_name: str,
    ) -> dict[str, Any]:
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.model,
            "max_tokens": 1200,
            "system": (
                f"{system}\n\n"
                f"Respond with ONLY valid JSON for schema `{schema_name}`. "
                "No markdown fences."
            ),
            "messages": [{"role": "user", "content": user}],
        }
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            resp = await client.post(self.base_url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()
        text = "".join(
            part.get("text", "") for part in data.get("content", []) if part.get("type") == "text"
        )
        return _parse_json_object(text)


def _parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("LLM did not return a JSON object")
    parsed = json.loads(text[start : end + 1])
    if isinstance(parsed, dict):
        return parsed
    # Callers treat a malformed LLM reply as ValueError and fall back to stubs.
    raise ValueError("LLM did not return a JSON object")
