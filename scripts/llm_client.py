#!/usr/bin/env python3
"""LLM client for pattern extraction.

Thin wrapper around OpenAI / compatible API.
Reuses same interface pattern as rv-optkb-tool's LLMClient.
"""

import json
import sys
from typing import Any


class LLMError(Exception):
    pass


class LLMClient:
    """Minimal LLM client for structured JSON responses."""

    def __init__(self, config: dict):
        self.model = config.get("model", "gpt-4o")
        self.api_key = config.get("api_key", "")
        self.base_url = config.get("base_url", None)
        self.temperature = config.get("temperature", 0.1)
        self.max_tokens = config.get("max_tokens", 4096)

        # Defer import so the module can be imported without openai installed
        import openai

        import httpx
        timeout_sec = config.get("timeout", 120)
        kwargs: dict[str, Any] = {
            "api_key": self.api_key,
            "http_client": httpx.Client(timeout=httpx.Timeout(timeout_sec)),
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        self._client = openai.OpenAI(**kwargs)

    def chat(self, system: str, user: str) -> str:
        """Simple chat completion, returns raw text."""
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            raise LLMError(f"LLM chat failed: {e}") from e

    def chat_json(self, system: str, user: str) -> dict:
        """Chat completion that returns parsed JSON."""
        content = self.chat(system, user)
        # Try to extract JSON from code fence if present
        content = content.strip()
        if content.startswith("```"):
            # Remove code fences
            lines = content.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines).strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError as e:
            raise LLMError(f"LLM response is not valid JSON: {e}\nRaw: {content[:500]}") from e

    def classify(self, system_prompt: str, user_content: str) -> dict:
        """Classify with strict JSON response."""
        result = self.chat_json(system_prompt, user_content)
        if not isinstance(result, dict):
            raise LLMError(f"Expected dict, got {type(result).__name__}")
        return result
