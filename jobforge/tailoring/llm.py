"""Optional OpenAI-compatible LLM client (Ollama, LM Studio, OpenAI, etc.)."""

from __future__ import annotations

import json
import re
from typing import Any

import requests

from ..config import LLMConfig


class LLMClient:
    def __init__(self, config: LLMConfig) -> None:
        self.config = config

    @property
    def available(self) -> bool:
        return self.config.configured

    def chat(self, system: str, user: str, *, json_mode: bool = False, temperature: float | None = None) -> str | None:
        if not self.available:
            return None
        url = self.config.base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.config.temperature if temperature is None else temperature,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=self.config.timeout)
            resp.raise_for_status()
            data = resp.json()
            return (data.get("choices") or [{}])[0].get("message", {}).get("content")
        except (requests.RequestException, ValueError, KeyError):
            return None

    def json_chat(self, system: str, user: str) -> Any | None:
        content = self.chat(system, user, json_mode=True)
        if not content:
            return None
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
        try:
            return json.loads(content)
        except ValueError:
            match = re.search(r"(\{.*\}|\[.*\])", content, re.DOTALL)
            if not match:
                return None
            try:
                return json.loads(match.group(1))
            except ValueError:
                return None


GROUNDING_SYSTEM = """You are a resume editor. You may reorder, shorten, and rephrase facts.
You must not invent employers, titles, dates, skills, metrics, degrees, certifications, or achievements.
Use only the facts provided. If a fact is not provided, omit it. Return the requested JSON only."""

COVER_LETTER_SYSTEM = """You write short, specific cover letters. Use only the candidate facts provided.
Never invent experience, metrics, or enthusiasm for a company you cannot verify. No clichés.
Return plain text, 120-180 words, with a greeting, two body paragraphs, and a sign-off."""
