"""Optional model client used by tailoring (summary and cover letter).

The agent uses the richer provider client directly. This adapter keeps the
historical ``LLMClient.chat`` / ``json_chat`` surface for tailoring so the
cover-letter fallback and grounding checks do not change.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..agent.providers import (
    ClaudeCodeClient,
    ProviderClient,
    ProviderCredentialStore,
    ProviderError,
    base_url_for,
    claude_code_status,
    get_provider,
    infer_provider,
)
from ..config import LLMConfig


class LLMClient:
    def __init__(self, config: LLMConfig | Any) -> None:
        self.config = getattr(config, "llm", config)
        self.data_dir = getattr(config, "data_dir", None)
        self._client: ProviderClient | ClaudeCodeClient | None = None
        self._error = ""
        self._build()

    def _build(self) -> None:
        if not self.config.enabled:
            self._error = "Model provider is disabled."
            return
        spec = get_provider(infer_provider(self.config))
        model = (self.config.model or "").strip()
        if not model and spec.fallback_models:
            model = spec.fallback_models[0]
        if spec.auth_mode == "cli":
            status = claude_code_status()
            if not status.authenticated:
                self._error = status.error or "Claude Code is not signed in."
                return
            if not model:
                self._error = f"{spec.label} needs a model."
                return
            self._client = ClaudeCodeClient(
                spec, model=model, timeout=self.config.timeout
            )
            return

        key = ""
        if self.data_dir is not None:
            record = ProviderCredentialStore(self.data_dir).load(spec.id)
            key = record.token if record else ""
        if not key:
            for name in spec.env_keys:
                value = os.environ.get(name, "").strip()
                if value:
                    key = value
                    break
        if not key:
            key = (self.config.api_key or "").strip()
        if spec.auth_mode in {"api_key", "oauth"} and not key:
            self._error = f"{spec.label} is not connected."
            return
        if not model:
            self._error = f"{spec.label} needs a model."
            return
        self._client = ProviderClient(
            spec,
            base_url=base_url_for(self.config, spec),
            api_key=key,
            model=model,
            timeout=self.config.timeout,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
        )

    @property
    def available(self) -> bool:
        return bool(self._client and self._client.configured)

    @property
    def model(self) -> str:
        return str(getattr(self.config, "model", "") or "")

    @property
    def error(self) -> str:
        return self._error

    def chat(
        self,
        system: str,
        user: str,
        *,
        json_mode: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str | None:
        if not self.available or self._client is None:
            return None
        try:
            result = self._client.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                json_mode=json_mode,
                temperature=(
                    self.config.temperature if temperature is None else temperature
                ),
                max_tokens=max_tokens,
            )
        except ProviderError as exc:
            self._error = str(exc)
            return None
        return result.content or None

    def json_chat(
        self, system: str, user: str, *, max_tokens: int | None = None
    ) -> Any | None:
        # Some OpenAI-compatible reasoning models spend the token budget on
        # reasoning and return truncated JSON. Retry once without JSON mode and
        # with a larger explicit budget; the parser below accepts fenced JSON.
        for json_mode in (True, False):
            content = self.chat(
                system,
                user,
                json_mode=json_mode,
                max_tokens=max_tokens,
            )
            if not content:
                continue
            content = re.sub(
                r"^```(?:json)?\s*|\s*```$",
                "",
                content.strip(),
                flags=re.IGNORECASE,
            )
            try:
                return json.loads(content)
            except ValueError:
                match = re.search(r"(\{.*\}|\[.*\])", content, re.DOTALL)
                if not match:
                    continue
                try:
                    return json.loads(match.group(1))
                except ValueError:
                    continue
        return None


GROUNDING_SYSTEM = """You are a resume editor. You may reorder, shorten, and rephrase facts.
You must not invent employers, titles, dates, skills, metrics, degrees, certifications, or achievements.
Use only the facts provided. If a fact is not provided, omit it. Return the requested JSON only."""

COVER_LETTER_SYSTEM = """You write short, specific cover letters. Use only the candidate facts provided.
Never invent experience, metrics, or enthusiasm for a company you cannot verify. No clichés.
Return plain text, 120-180 words, with a greeting, two body paragraphs, and a sign-off."""
