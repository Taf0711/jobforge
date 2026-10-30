"""Local model-provider registry, credential storage, and chat clients.

The provider layer is intentionally small. It supports two wire protocols:

- OpenAI-compatible ``/chat/completions`` (OpenAI, OpenRouter, Groq, Mistral,
  xAI, DeepSeek, Together, Fireworks, Baseten, Gemini compatibility, Ollama,
  LM Studio, custom gateways).
- Anthropic ``/v1/messages``.

Secrets never enter SQLite, templates, exports, logs, or ``config.yml``. The
storage order is OS keychain, a 0600 file, then an environment variable.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import requests

from .. import db
from ..config import Config, LLMConfig
from ..paths import default_config_path

KEYRING_SERVICE = "jobforge.llm"
ANTHROPIC_VERSION = "2023-06-01"
OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"
OAUTH_PENDING_FILE = "oauth_pending.json"
OAUTH_TTL_SECONDS = 600
MAX_KEY_CHARS = 4096
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class ProviderError(RuntimeError):
    """A user-facing provider operation failed."""


class ProviderAuthError(ProviderError):
    """The provider is missing a credential or rejected it."""


class ProviderRateLimitError(ProviderError):
    """The provider rate limit or quota was hit."""


class ProviderModelError(ProviderError):
    """The requested model is unavailable or the provider rejected the request."""


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    label: str
    protocol: str
    default_base_url: str
    kind: str = "hosted"  # hosted | local | custom
    auth_mode: str = "api_key"  # api_key | oauth | local | optional | cli
    env_keys: tuple[str, ...] = ()
    key_url: str = ""
    docs_url: str = ""
    fallback_models: tuple[str, ...] = ()
    supports_oauth: bool = False
    custom_base_url: bool = False


PROVIDERS: dict[str, ProviderSpec] = {
    "openai": ProviderSpec(
        id="openai",
        label="OpenAI / ChatGPT",
        protocol="openai",
        default_base_url="https://api.openai.com/v1",
        env_keys=("OPENAI_API_KEY", "JOBFORGE_LLM_API_KEY"),
        key_url="https://platform.openai.com/api-keys",
        docs_url="https://platform.openai.com/docs",
        fallback_models=("gpt-5.2", "gpt-5.2-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"),
    ),
    "claude_code": ProviderSpec(
        id="claude_code",
        label="Claude Code (subscription CLI)",
        protocol="cli",
        default_base_url="",
        kind="cli",
        auth_mode="cli",
        docs_url="https://code.claude.com/docs/en/authentication",
        fallback_models=("sonnet", "opus", "haiku"),
    ),
    "anthropic": ProviderSpec(
        id="anthropic",
        label="Anthropic / Claude",
        protocol="anthropic",
        default_base_url="https://api.anthropic.com",
        env_keys=("ANTHROPIC_API_KEY",),
        key_url="https://platform.claude.com/settings/keys",
        docs_url="https://platform.claude.com/docs",
        fallback_models=("claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"),
    ),
    "gemini": ProviderSpec(
        id="gemini",
        label="Google Gemini",
        protocol="openai",
        default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_keys=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        key_url="https://aistudio.google.com/apikey",
        docs_url="https://ai.google.dev/gemini-api/docs/openai",
        fallback_models=("gemini-2.5-pro", "gemini-2.5-flash"),
    ),
    "openrouter": ProviderSpec(
        id="openrouter",
        label="OpenRouter",
        protocol="openai",
        default_base_url="https://openrouter.ai/api/v1",
        auth_mode="oauth",
        env_keys=("OPENROUTER_API_KEY",),
        key_url="https://openrouter.ai/settings/keys",
        docs_url="https://openrouter.ai/docs",
        fallback_models=(
            "openai/gpt-4.1-mini",
            "anthropic/claude-sonnet-4.5",
            "google/gemini-2.5-flash",
        ),
        supports_oauth=True,
    ),
    "groq": ProviderSpec(
        id="groq",
        label="Groq",
        protocol="openai",
        default_base_url="https://api.groq.com/openai/v1",
        env_keys=("GROQ_API_KEY",),
        key_url="https://console.groq.com/keys",
        fallback_models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant"),
    ),
    "mistral": ProviderSpec(
        id="mistral",
        label="Mistral",
        protocol="openai",
        default_base_url="https://api.mistral.ai/v1",
        env_keys=("MISTRAL_API_KEY",),
        key_url="https://console.mistral.ai/api-keys",
        fallback_models=("mistral-large-latest", "mistral-small-latest"),
    ),
    "xai": ProviderSpec(
        id="xai",
        label="xAI",
        protocol="openai",
        default_base_url="https://api.x.ai/v1",
        env_keys=("XAI_API_KEY",),
        key_url="https://console.x.ai",
        fallback_models=("grok-4", "grok-3-mini"),
    ),
    "deepseek": ProviderSpec(
        id="deepseek",
        label="DeepSeek",
        protocol="openai",
        default_base_url="https://api.deepseek.com",
        env_keys=("DEEPSEEK_API_KEY",),
        key_url="https://platform.deepseek.com/api_keys",
        fallback_models=("deepseek-chat", "deepseek-reasoner"),
    ),
    "together": ProviderSpec(
        id="together",
        label="Together AI",
        protocol="openai",
        default_base_url="https://api.together.xyz/v1",
        env_keys=("TOGETHER_API_KEY",),
        key_url="https://api.together.ai/settings/api-keys",
        fallback_models=("meta-llama/Llama-3.3-70B-Instruct-Turbo",),
    ),
    "fireworks": ProviderSpec(
        id="fireworks",
        label="Fireworks AI",
        protocol="openai",
        default_base_url="https://api.fireworks.ai/inference/v1",
        env_keys=("FIREWORKS_API_KEY",),
        key_url="https://fireworks.ai/account/api-keys",
        fallback_models=("accounts/fireworks/models/llama-v3p3-70b-instruct",),
    ),
    "baseten": ProviderSpec(
        id="baseten",
        label="Baseten",
        protocol="openai",
        default_base_url="https://inference.baseten.co/v1",
        env_keys=("BASETEN_API_KEY",),
        key_url="https://app.baseten.co/settings/account/api_keys",
        fallback_models=("deepseek-ai/DeepSeek-V3",),
    ),
    "ollama": ProviderSpec(
        id="ollama",
        label="Ollama (local)",
        protocol="openai",
        default_base_url="http://localhost:11434/v1",
        kind="local",
        auth_mode="local",
        docs_url="https://docs.ollama.com",
        fallback_models=("llama3.1:8b", "qwen2.5-coder:7b"),
    ),
    "lmstudio": ProviderSpec(
        id="lmstudio",
        label="LM Studio (local)",
        protocol="openai",
        default_base_url="http://localhost:1234/v1",
        kind="local",
        auth_mode="local",
        docs_url="https://lmstudio.ai/docs",
        fallback_models=("local-model",),
    ),
    "custom": ProviderSpec(
        id="custom",
        label="Custom OpenAI-compatible",
        protocol="openai",
        default_base_url="",
        kind="custom",
        auth_mode="optional",
        env_keys=("JOBFORGE_LLM_API_KEY",),
        fallback_models=(),
        custom_base_url=True,
    ),
}

ALIASES = {
    "chatgpt": "openai",
    "open-ai": "openai",
    "claude": "anthropic",
    "google": "gemini",
    "google-gemini": "gemini",
    "open-router": "openrouter",
    "claude-code": "claude_code",
    "claude_code": "claude_code",
    "local": "ollama",
}


def normalize_provider_id(value: str) -> str:
    key = (value or "").strip().lower()
    key = ALIASES.get(key, key)
    return key if key in PROVIDERS else ""


def get_provider(value: str) -> ProviderSpec:
    provider_id = normalize_provider_id(value)
    if not provider_id:
        raise ProviderError(f"Unknown provider: {value}")
    return PROVIDERS[provider_id]


def infer_provider(config: LLMConfig) -> str:
    explicit = normalize_provider_id(config.provider)
    if explicit:
        return explicit
    url = (config.base_url or "").lower()
    if "openrouter.ai" in url:
        return "openrouter"
    if "api.anthropic.com" in url:
        return "anthropic"
    if "generativelanguage.googleapis.com" in url:
        return "gemini"
    if "api.openai.com" in url:
        return "openai"
    if ":11434" in url:
        return "ollama"
    if ":1234" in url:
        return "lmstudio"
    return "custom"


def base_url_for(config: LLMConfig, spec: ProviderSpec) -> str:
    """Resolve a base URL without keeping a stale default from another provider.

    A user can set ``llm.provider: openai`` while the example's default
    ``base_url`` still points at Ollama. Only keep an explicit base URL when it
    is that provider's own default or a genuinely custom endpoint.
    """
    raw = (config.base_url or "").strip().rstrip("/")
    if not raw:
        return spec.default_base_url
    known = {
        item.default_base_url.rstrip("/")
        for item in PROVIDERS.values()
        if item.default_base_url
    }
    if raw == spec.default_base_url.rstrip("/"):
        return raw
    if raw not in known:
        return raw
    return spec.default_base_url


@dataclass
class CredentialRecord:
    token: str
    backend: str
    warning: str = ""


def _load_keyring() -> Any:
    try:
        import keyring  # type: ignore

        return keyring
    except Exception:  # noqa: BLE001  # pragma: no cover - optional dependency
        return None


def _safe_provider_file(provider_id: str) -> str:
    return "".join(ch for ch in provider_id if ch.isalnum() or ch in "._-") or "provider"


def _validated_key(value: str) -> str:
    key = (value or "").strip()
    if not key:
        raise ProviderAuthError("Enter an API key.")
    if len(key) > MAX_KEY_CHARS or any(ch.isspace() for ch in key):
        raise ProviderAuthError("That does not look like a provider API key.")
    return key


@dataclass(frozen=True)
class ClaudeCodeStatus:
    available: bool
    authenticated: bool
    auth_method: str = ""
    version: str = ""
    path: str = ""
    error: str = ""


def claude_code_status() -> ClaudeCodeStatus:
    """Check the official Claude Code CLI without reading its credentials."""
    path = shutil.which("claude") or ""
    if not path:
        return ClaudeCodeStatus(
            available=False,
            authenticated=False,
            error="Claude Code CLI not found on PATH. Install it, then run `claude auth login`.",
        )
    version = ""
    try:
        version_proc = subprocess.run(
            [path, "--version"], text=True, capture_output=True, timeout=10, check=False
        )
        if version_proc.stdout.strip():
            version = version_proc.stdout.strip().splitlines()[0][:80]
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc = subprocess.run(
            [path, "auth", "status"],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ClaudeCodeStatus(
            available=True,
            authenticated=False,
            version=version,
            path=path,
            error=f"Could not check Claude Code sign-in: {exc}",
        )
    if proc.returncode != 0:
        return ClaudeCodeStatus(
            available=True,
            authenticated=False,
            version=version,
            path=path,
            error="Claude Code is not signed in. Run `claude auth login` in a terminal.",
        )
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return ClaudeCodeStatus(
            available=True,
            authenticated=False,
            version=version,
            path=path,
            error="Claude Code auth status was not valid JSON.",
        )
    auth_method = str(data.get("authMethod") or "")
    authenticated = bool(data.get("loggedIn")) and auth_method not in {"", "none"}
    return ClaudeCodeStatus(
        available=True,
        authenticated=authenticated,
        auth_method=auth_method,
        version=version,
        path=path,
        error="" if authenticated else "Claude Code is not signed in. Run `claude auth login` in a terminal.",
    )


class ProviderCredentialStore:
    """Keychain-first credential storage for one provider namespace."""

    def __init__(self, data_dir: str | Path, *, keyring_module: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.directory = self.data_dir / "connections" / "providers"
        if keyring_module is False:
            self._keyring = None
        else:
            self._keyring = _load_keyring() if keyring_module is None else keyring_module
        self._keyring_error = ""
        if keyring_module is None and self._keyring is not None:
            try:
                backend = self._keyring.get_keyring()
                if "fail" in str(backend).lower():
                    self._keyring = None
                    self._keyring_error = "No usable OS keychain backend"
            except Exception as exc:  # noqa: BLE001  # pragma: no cover
                self._keyring = None
                self._keyring_error = str(exc)

    @property
    def keyring_available(self) -> bool:
        return self._keyring is not None

    def _path(self, provider_id: str) -> Path:
        return self.directory / f"{_safe_provider_file(provider_id)}.key"

    def _env_key(self, provider_id: str) -> str:
        spec = get_provider(provider_id)
        for name in spec.env_keys:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return ""

    def load(self, provider_id: str, backend: str = "") -> CredentialRecord | None:
        spec = get_provider(provider_id)
        if spec.auth_mode in {"local", "cli"}:
            return CredentialRecord(token="", backend=spec.auth_mode)
        backend = (backend or "").strip()
        if backend in {"", "keyring"} and self._keyring is not None:
            try:
                token = self._keyring.get_password(
                    f"{KEYRING_SERVICE}.{spec.id}", "default"
                )
                if token:
                    return CredentialRecord(token=str(token).strip(), backend="keyring")
            except Exception as exc:  # noqa: BLE001
                self._keyring_error = str(exc)
        if backend in {"", "file"}:
            path = self._path(spec.id)
            try:
                if path.is_file():
                    token = path.read_text(encoding="utf-8").strip()
                    if token:
                        return CredentialRecord(
                            token=token,
                            backend="file",
                            warning=(
                                "Key is stored in a local 0600 file; install an OS "
                                "keychain backend to encrypt it at rest."
                            ),
                        )
            except OSError:
                pass
        if backend in {"", "env"}:
            token = self._env_key(spec.id)
            if token:
                return CredentialRecord(token=token, backend="env")
        return None

    def save(self, provider_id: str, key: str) -> CredentialRecord:
        spec = get_provider(provider_id)
        if spec.auth_mode == "cli":
            raise ProviderAuthError(
                "Claude Code uses its own sign-in. Run `claude auth login` in a terminal; "
                "JobForge never stores or reads that credential."
            )
        token = _validated_key(key)
        if self._keyring is not None:
            try:
                self._keyring.set_password(
                    f"{KEYRING_SERVICE}.{spec.id}", "default", token
                )
                self._remove_file(spec.id)
                return CredentialRecord(token=token, backend="keyring")
            except Exception as exc:  # noqa: BLE001
                self._keyring_error = str(exc)
        self._write_file(spec.id, token)
        return CredentialRecord(
            token=token,
            backend="file",
            warning="OS keychain unavailable; key is stored in a local 0600 file.",
        )

    def delete(self, provider_id: str) -> None:
        spec = get_provider(provider_id)
        if spec.auth_mode == "cli":
            # The CLI owns its credential; there is nothing in this store to delete.
            return
        if self._keyring is not None:
            try:
                self._keyring.delete_password(
                    f"{KEYRING_SERVICE}.{spec.id}", "default"
                )
            except Exception as exc:  # noqa: BLE001
                self._keyring_error = str(exc)
        self._remove_file(spec.id)

    def _write_file(self, provider_id: str, token: str) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        path = self._path(provider_id)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{_safe_provider_file(provider_id)}-", dir=str(self.directory), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token + "\n")
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _remove_file(self, provider_id: str) -> None:
        try:
            self._path(provider_id).unlink(missing_ok=True)
        except OSError:
            pass


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""
    model: str = ""


class ProviderClient:
    """Restricted HTTP client for one provider and one selected model."""

    def __init__(
        self,
        spec: ProviderSpec,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 120,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        session: requests.Session | None = None,
    ) -> None:
        self.spec = spec
        self.base_url = _validated_base_url(base_url, spec)
        self._api_key = (api_key or "").strip()
        self.model = model.strip()
        self.timeout = max(5, min(600, int(timeout)))
        self.temperature = max(0.0, min(2.0, float(temperature)))
        self.max_tokens = max(64, min(128_000, int(max_tokens)))
        self.session = session or requests.Session()

    @property
    def configured(self) -> bool:
        return bool(self.model) and (self.spec.auth_mode in {"local", "optional"} or bool(self._api_key))

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "JobForge/0.5 (+https://github.com/Taf0711/jobforge)",
        }
        if self.spec.protocol == "anthropic":
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            headers["anthropic-version"] = ANTHROPIC_VERSION
        elif self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.session.request(
                method,
                url,
                json=json_body,
                headers={**self._headers(), **(headers or {})},
                params=params,
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise ProviderError(f"Could not reach {self.spec.label}: {exc}") from None
        try:
            body = _read_capped(response)
            if response.status_code in {301, 302, 303, 307, 308}:
                raise ProviderError(
                    f"{self.spec.label} returned a redirect; provider redirects are disabled."
                )
            if response.status_code == 401:
                raise ProviderAuthError(f"{self.spec.label} rejected the API key (401).")
            if response.status_code == 403:
                raise ProviderAuthError(
                    f"{self.spec.label} denied access (403). Check model access and account permissions."
                )
            if response.status_code == 429:
                raise ProviderRateLimitError(
                    f"{self.spec.label} rate limit or quota reached (429)."
                )
            if response.status_code >= 400:
                detail = _error_detail(body)
                if self._api_key:
                    detail = detail.replace(self._api_key, "[redacted]")
                raise ProviderModelError(
                    f"{self.spec.label} returned HTTP {response.status_code}: {detail}"
                )
            try:
                value = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ProviderError(
                    f"{self.spec.label} returned a non-JSON response."
                ) from None
            if not isinstance(value, dict):
                raise ProviderError(f"{self.spec.label} returned an unexpected response shape.")
            return value
        finally:
            response.close()

    def list_models(self) -> list[str]:
        if self.spec.id == "gemini":
            models = self._list_gemini()
        elif self.spec.protocol == "anthropic":
            models = self._list_anthropic()
        elif self.spec.id == "ollama":
            models = self._list_ollama()
        else:
            models = self._list_openai_compatible()
        seen: set[str] = set()
        out: list[str] = []
        for model in models:
            if model and model not in seen:
                seen.add(model)
                out.append(model)
        if out:
            return out[:500]
        if self.spec.fallback_models:
            return list(self.spec.fallback_models)
        raise ProviderModelError(
            f"{self.spec.label} did not return a model list. Enter a model ID manually."
        )

    def _list_openai_compatible(self) -> list[str]:
        url = self.base_url.rstrip("/") + "/models"
        try:
            payload = self._request("GET", url)
        except ProviderAuthError:
            raise
        except ProviderError as exc:
            if self.spec.id in {"openai", "openrouter", "groq", "mistral", "xai", "deepseek", "together", "fireworks", "baseten"}:
                # The model-list endpoint is useful but not guaranteed. Report a
                # fallback list; authentication failures above still surface.
                return list(self.spec.fallback_models)
            raise ProviderError(str(exc)) from None
        data = payload.get("data") or payload.get("models") or []
        if isinstance(data, dict):
            data = list(data.values())
        out: list[str] = []
        for item in data:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                for key in ("id", "name", "model"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        out.append(value.strip())
                        break
        return out

    def _list_anthropic(self) -> list[str]:
        url = self.base_url.rstrip("/") + "/v1/models"
        payload = self._request("GET", url, params={"limit": 100})
        data = payload.get("data") or []
        return [
            str(item.get("id"))
            for item in data
            if isinstance(item, dict) and item.get("id")
        ]

    def _list_gemini(self) -> list[str]:
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        headers = {}
        if self._api_key:
            headers["x-goog-api-key"] = self._api_key
        payload = self._request("GET", url, headers=headers, params={"pageSize": 200})
        out: list[str] = []
        for item in payload.get("models") or []:
            if not isinstance(item, dict):
                continue
            methods = item.get("supportedGenerationMethods") or []
            if methods and "generateContent" not in methods:
                continue
            name = str(item.get("name") or "")
            if name.startswith("models/"):
                name = name.split("/", 1)[1]
            if name:
                out.append(name)
        return out

    def _list_ollama(self) -> list[str]:
        base = self.base_url.rstrip("/")
        base = base.removesuffix("/v1")
        payload = self._request("GET", base.rstrip("/") + "/api/tags")
        out: list[str] = []
        for item in payload.get("models") or []:
            if isinstance(item, dict):
                name = str(item.get("name") or item.get("model") or "").strip()
                if name:
                    out.append(name)
        return out

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        if self.spec.protocol == "anthropic":
            return self._anthropic_chat(
                messages,
                tools=tools,
                max_tokens=max_tokens,
                temperature=temperature,
                json_mode=json_mode,
            )
        return self._openai_chat(
            messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            json_mode=json_mode,
        )

    def _openai_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None,
        temperature: float | None,
        json_mode: bool = False,
    ) -> ChatResult:
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [_openai_message(message) for message in messages],
            "temperature": self.temperature if temperature is None else temperature,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            data = self._request("POST", url, json_body=payload)
        except ProviderModelError as exc:
            message = str(exc).lower()
            if "max_tokens" in message and "max_completion_tokens" not in payload:
                payload["max_completion_tokens"] = payload.pop("max_tokens")
                data = self._request("POST", url, json_body=payload)
            elif "temperature" in message:
                payload.pop("temperature", None)
                data = self._request("POST", url, json_body=payload)
            else:
                raise
        choices = data.get("choices") or []
        if not choices or not isinstance(choices[0], dict):
            raise ProviderError(f"{self.spec.label} returned no completion choices.")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
        tool_calls: list[dict[str, Any]] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            if not isinstance(call, dict):
                continue
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                arguments = {"_parse_error": str(raw_args)[:500]}
            tool_calls.append(
                {
                    "id": str(call.get("id") or f"call_{index}"),
                    "name": name,
                    "arguments": arguments if isinstance(arguments, dict) else {},
                }
            )
        usage = data.get("usage") or {}
        return ChatResult(
            content=str(content).strip(),
            tool_calls=tool_calls,
            usage={
                "input_tokens": int(usage.get("prompt_tokens") or 0),
                "output_tokens": int(usage.get("completion_tokens") or 0),
                "total_tokens": int(usage.get("total_tokens") or 0),
            },
            finish_reason=str(choices[0].get("finish_reason") or ""),
            model=str(data.get("model") or self.model),
        )

    def _anthropic_chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None,
        max_tokens: int | None,
        temperature: float | None,
        json_mode: bool = False,
    ) -> ChatResult:
        system, converted = _anthropic_messages(messages)
        if json_mode:
            system = (system + "\n\nReturn only valid JSON. Do not wrap it in Markdown.").strip()
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens if max_tokens is None else max_tokens,
            "temperature": self.temperature if temperature is None else temperature,
            "messages": converted,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": item["function"]["name"],
                    "description": item["function"].get("description", ""),
                    "input_schema": item["function"].get("parameters", {"type": "object"}),
                }
                for item in tools
                if isinstance(item, dict) and item.get("type") == "function"
            ]
        url = self.base_url.rstrip("/") + "/v1/messages"
        data = self._request("POST", url, json_body=payload)
        blocks = data.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text_parts.append(str(block.get("text") or ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": str(block.get("id") or ""),
                        "name": str(block.get("name") or ""),
                        "arguments": block.get("input") if isinstance(block.get("input"), dict) else {},
                    }
                )
        usage = data.get("usage") or {}
        return ChatResult(
            content="\n".join(text_parts).strip(),
            tool_calls=tool_calls,
            usage={
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
                "total_tokens": int(usage.get("input_tokens") or 0)
                + int(usage.get("output_tokens") or 0),
            },
            finish_reason=str(data.get("stop_reason") or ""),
            model=str(data.get("model") or self.model),
        )

    def test_chat(self) -> ChatResult:
        return self.chat(
            [{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=16,
            temperature=0,
        )


def _read_capped(response: requests.Response) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=16384):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ProviderError("Provider response exceeded the local safety limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def _error_detail(body: bytes) -> str:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body.decode("utf-8", errors="replace")[:300]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("type") or error.get("code")
            if message:
                return str(message)[:300]
        if isinstance(error, str):
            return error[:300]
        message = payload.get("message")
        if message:
            return str(message)[:300]
    return "provider error"


class ClaudeCodeClient:
    """Text-only bridge to the official Claude Code CLI.

    JobForge never reads Claude Code's credential files or Keychain entries.
    The CLI authenticates itself; this client only passes a prompt and parses
    the JSON result. Tools are disabled, so the agent cannot write or submit.
    """

    supports_tools = False

    def __init__(
        self,
        spec: ProviderSpec,
        *,
        model: str,
        timeout: int = 120,
        binary: str = "",
    ) -> None:
        self.spec = spec
        self.model = (model or "").strip()
        self.timeout = max(30, min(900, int(timeout)))
        self.binary = (binary or shutil.which("claude") or "").strip()

    @property
    def configured(self) -> bool:
        return bool(self.model and self.binary and claude_code_status().authenticated)

    def list_models(self) -> list[str]:
        return list(self.spec.fallback_models)

    def test_chat(self) -> ChatResult:
        return self.chat([{"role": "user", "content": "Reply with exactly: OK"}])

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        json_mode: bool = False,
    ) -> ChatResult:
        status = claude_code_status()
        if not status.available:
            raise ProviderAuthError(status.error or "Claude Code CLI is not available.")
        if not status.authenticated:
            raise ProviderAuthError(
                status.error
                or "Claude Code is not signed in. Run `claude auth login` in a terminal."
            )
        if not self.model:
            raise ProviderModelError(
                "Choose a Claude Code model alias (sonnet, opus, or haiku) or a full model ID."
            )
        system, prompt = _claude_code_messages(messages)
        if json_mode:
            system = (
                system + "\n\nReturn only valid JSON. Do not wrap it in Markdown."
            ).strip()
        if tools:
            system = (
                system
                + "\n\nJobForge tools are unavailable in this text-only bridge. "
                "Answer from the candidate context already provided; do not claim to "
                "have called a tool or changed any data."
            ).strip()

        marker = "\nCANDIDATE CONTEXT:\n"
        if marker in system:
            head, context = system.split(marker, 1)
            prompt = "CANDIDATE CONTEXT:\n" + context + "\n\n" + prompt
            system = head.strip()

        command = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--model",
            self.model,
            "--tools",
            "",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--permission-mode",
            "dontAsk",
            "--no-session-persistence",
        ]
        if system:
            command.extend(["--append-system-prompt", system])
        try:
            with tempfile.TemporaryDirectory(prefix="jobforge-claude-") as cwd:
                proc = subprocess.run(
                    command,
                    input=prompt or "Continue.",
                    text=True,
                    capture_output=True,
                    timeout=self.timeout,
                    cwd=cwd,
                    check=False,
                )
        except subprocess.TimeoutExpired:
            raise ProviderError(
                f"Claude Code did not answer within {self.timeout}s."
            ) from None
        except OSError as exc:
            raise ProviderError(f"Could not start Claude Code: {exc}") from None
        if proc.returncode != 0:
            lines = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = lines[-1] if lines else f"exit code {proc.returncode}"
            raise ProviderError(f"Claude Code failed: {detail[:300]}")
        try:
            payload = json.loads(proc.stdout or "{}")
        except ValueError:
            raise ProviderError("Claude Code returned a non-JSON response.") from None
        if not isinstance(payload, dict):
            raise ProviderError("Claude Code returned an unexpected response shape.")
        if payload.get("is_error"):
            message = str(payload.get("result") or "unknown error")
            raise ProviderError(f"Claude Code error: {message[:300]}")
        content = str(payload.get("result") or "").strip()
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        return ChatResult(
            content=content,
            tool_calls=[],
            usage={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            finish_reason="stop",
            model=self.model,
        )


def _claude_code_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, str]:
    system_parts: list[str] = []
    conversation: list[str] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = str(message.get("content") or "").strip()
        if role == "system":
            if content:
                system_parts.append(content)
        elif role == "assistant":
            if content:
                conversation.append(f"Assistant:\n{content}")
            for call in message.get("tool_calls") or []:
                if isinstance(call, dict):
                    conversation.append(
                        "Assistant tool call: "
                        + str(call.get("name") or "unknown")
                        + "("
                        + json.dumps(call.get("arguments") or {}, ensure_ascii=False)
                        + ")"
                    )
        elif role == "tool":
            name = str(message.get("name") or "tool")
            conversation.append(f"Tool result ({name}):\n{content}")
        elif content:
            conversation.append(f"User:\n{content}")
    return "\n\n".join(system_parts).strip(), "\n\n".join(conversation).strip()


def make_provider_client(
    spec: ProviderSpec,
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: int = 120,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> ProviderClient | ClaudeCodeClient:
    if spec.auth_mode == "cli":
        return ClaudeCodeClient(spec, model=model, timeout=timeout)
    return ProviderClient(
        spec,
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _validated_base_url(value: str, spec: ProviderSpec) -> str:
    if spec.auth_mode == "cli":
        return ""
    raw = (value or spec.default_base_url or "").strip().rstrip("/")
    if not raw:
        raise ProviderError(f"{spec.label} needs a base URL.")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderError(f"{spec.label} base URL must be an http(s) URL with a hostname.")
    if parsed.username or parsed.password:
        raise ProviderError("Provider base URL must not contain credentials.")
    return raw


def _openai_message(message: dict[str, Any]) -> dict[str, Any]:
    role = str(message.get("role") or "user")
    out: dict[str, Any] = {"role": role}
    if role == "tool":
        out["tool_call_id"] = str(message.get("tool_call_id") or "")
        out["content"] = str(message.get("content") or "")
        return out
    if role == "assistant" and message.get("tool_calls"):
        out["content"] = message.get("content") or None
        out["tool_calls"] = [
            {
                "id": str(call.get("id") or f"call_{index}"),
                "type": "function",
                "function": {
                    "name": str(call.get("name") or ""),
                    "arguments": json.dumps(call.get("arguments") or {}, ensure_ascii=False),
                },
            }
            for index, call in enumerate(message.get("tool_calls") or [])
        ]
        return out
    out["content"] = str(message.get("content") or "")
    return out


def _anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = str(message.get("role") or "user")
        if role == "system":
            system_parts.append(str(message.get("content") or ""))
            continue
        if role == "tool":
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(message.get("tool_call_id") or ""),
                            "content": str(message.get("content") or ""),
                        }
                    ],
                }
            )
            continue
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if message.get("content"):
                blocks.append({"type": "text", "text": str(message.get("content"))})
            for call in message.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id") or ""),
                        "name": str(call.get("name") or ""),
                        "input": call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                    }
                )
            converted.append({"role": "assistant", "content": blocks})
            continue
        converted.append(
            {
                "role": "assistant" if role == "assistant" else "user",
                "content": str(message.get("content") or ""),
            }
        )
    return "\n\n".join(part for part in system_parts if part.strip()), converted


@dataclass
class ResolvedProvider:
    spec: ProviderSpec
    base_url: str
    model: str
    api_key: str
    backend: str
    client: ProviderClient | ClaudeCodeClient
    connection: dict[str, Any] | None = None


def resolve_provider(
    config: Config,
    conn: Any | None = None,
    *,
    provider_id: str | None = None,
    model: str | None = None,
    store: ProviderCredentialStore | None = None,
) -> ResolvedProvider:
    """Resolve the active provider, credential source, base URL, and model."""
    config_provider = infer_provider(config.llm)
    selected = normalize_provider_id(provider_id or "") or config_provider
    spec = get_provider(selected)
    if not config.llm.enabled:
        raise ProviderAuthError(
            "Model provider is disabled. Open Models and choose a model to enable it."
        )
    if spec.auth_mode == "cli":
        status = claude_code_status()
        if not status.authenticated:
            raise ProviderAuthError(
                status.error
                or "Claude Code is not signed in. Run `claude auth login` in a terminal."
            )
    connection = (
        db.get_connection(conn, f"llm:{spec.id}") if conn is not None else None
    )
    meta = (connection or {}).get("meta") or {}
    base_url = str(meta.get("base_url") or "").strip()
    if not base_url:
        if config_provider == spec.id:
            base_url = base_url_for(config.llm, spec)
        else:
            base_url = spec.default_base_url
    store = store or ProviderCredentialStore(config.data_dir)
    record = store.load(spec.id, str((connection or {}).get("token_backend") or ""))
    api_key = record.token if record else ""
    backend = record.backend if record else ""
    if not api_key and config_provider == spec.id and config.llm.api_key:
        api_key = config.llm.api_key
        backend = "config"
    if not api_key and spec.auth_mode in {"api_key", "oauth"}:
        raise ProviderAuthError(
            f"{spec.label} is not connected. Open Models to connect a key."
        )
    if not model:
        if meta.get("model"):
            model = str(meta["model"])
        elif config_provider == spec.id and config.llm.model:
            model = config.llm.model
        elif spec.fallback_models:
            model = spec.fallback_models[0]
        else:
            raise ProviderModelError(
                f"{spec.label} needs a model. Open Models and choose one."
            )
    client = make_provider_client(
        spec,
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=config.llm.timeout,
        temperature=config.llm.temperature,
        max_tokens=config.llm.max_tokens,
    )
    return ResolvedProvider(
        spec=spec,
        base_url=base_url,
        model=model,
        api_key=api_key,
        backend=backend,
        client=client,
        connection=connection,
    )


def provider_status(
    config: Config,
    conn: Any,
    *,
    store: ProviderCredentialStore | None = None,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Return display-safe status for every provider card."""
    store = store or ProviderCredentialStore(config.data_dir)
    active_provider = infer_provider(config.llm)
    out: list[dict[str, Any]] = []
    for spec in PROVIDERS.values():
        connection = db.get_connection(conn, f"llm:{spec.id}")
        meta = (connection or {}).get("meta") or {}
        record = store.load(spec.id, str((connection or {}).get("token_backend") or ""))
        cli_status = claude_code_status() if spec.auth_mode == "cli" else None
        models = list(meta.get("models") or spec.fallback_models)
        if refresh and record and spec.auth_mode not in {"local", "cli"}:
            try:
                client = make_provider_client(
                    spec,
                    base_url=str(meta.get("base_url") or spec.default_base_url),
                    api_key=record.token,
                    model=str(meta.get("model") or (spec.fallback_models[0] if spec.fallback_models else "model")),
                    timeout=config.llm.timeout,
                    temperature=config.llm.temperature,
                    max_tokens=config.llm.max_tokens,
                )
                models = client.list_models()
                if connection:
                    db.save_connection(
                        conn,
                        f"llm:{spec.id}",
                        account_id=str(connection.get("account_id") or ""),
                        login=str(connection.get("login") or ""),
                        display_name=str(connection.get("display_name") or ""),
                        scopes=str(connection.get("scopes") or ""),
                        token_backend=str(connection.get("token_backend") or ""),
                        meta={**meta, "models": models, "base_url": meta.get("base_url") or spec.default_base_url, "checked_at": _now_iso()},
                    )
            except ProviderError:
                pass
        if cli_status is not None:
            connected = cli_status.authenticated
            backend = "cli" if connected else ""
            warning = "" if connected else cli_status.error
        else:
            connected = bool(record) or spec.auth_mode == "local" or (
                spec.auth_mode == "optional" and bool(meta.get("base_url"))
            )
            backend = (
                record.backend
                if record
                else (
                    "local"
                    if spec.kind == "local"
                    else ("optional" if spec.auth_mode == "optional" else "")
                )
            )
            warning = record.warning if record else ""
        out.append(
            {
                "spec": spec,
                "connected": connected,
                "backend": backend,
                "warning": warning,
                "base_url": "" if spec.auth_mode == "cli" else str(meta.get("base_url") or spec.default_base_url),
                "models": models if spec.auth_mode != "cli" else list(spec.fallback_models),
                "active": active_provider == spec.id,
                "active_model": config.llm.model if active_provider == spec.id else "",
                "connection": connection,
                "cli": (
                    {
                        "available": cli_status.available,
                        "authenticated": cli_status.authenticated,
                        "auth_method": cli_status.auth_method,
                        "version": cli_status.version,
                        "error": cli_status.error,
                    }
                    if cli_status is not None
                    else None
                ),
            }
        )
    return out


def remember_provider_connection(
    conn: Any,
    spec: ProviderSpec,
    *,
    backend: str,
    base_url: str,
    models: list[str] | None = None,
    model: str = "",
    account_id: str = "",
    login: str = "",
    display_name: str = "",
) -> None:
    base_url = _validated_base_url(base_url, spec)
    previous = db.get_connection(conn, f"llm:{spec.id}")
    meta = dict((previous or {}).get("meta") or {})
    meta.update(
        {
            "base_url": base_url,
            "models": list(models or meta.get("models") or []),
            "model": model or meta.get("model") or "",
            "auth_mode": spec.auth_mode,
            "checked_at": _now_iso(),
        }
    )
    db.save_connection(
        conn,
        f"llm:{spec.id}",
        account_id=account_id,
        login=login,
        display_name=display_name,
        scopes="",
        token_backend=backend,
        meta=meta,
    )


def persist_llm_config(
    config: Config,
    *,
    provider: str,
    model: str,
    base_url: str = "",
    enabled: bool = True,
) -> Path:
    """Atomically save the active selection. Never writes a credential."""
    import yaml

    spec = get_provider(provider)
    resolved_base = _validated_base_url(base_url or spec.default_base_url, spec)
    path = Path(config.config_path) if config.config_path else default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, yaml.YAMLError):
            data = {}
    llm = data.get("llm") if isinstance(data.get("llm"), dict) else {}
    llm.update(
        {
            "enabled": bool(enabled),
            "provider": spec.id,
            "model": model.strip(),
            "base_url": resolved_base,
        }
    )
    llm.pop("api_key", None)
    data["llm"] = llm
    fd, temp_name = tempfile.mkstemp(prefix=".config-", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, sort_keys=False, allow_unicode=True)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    config.llm.enabled = bool(enabled)
    config.llm.provider = spec.id
    config.llm.model = model.strip()
    config.llm.base_url = resolved_base
    config.llm.api_key = ""
    config.config_path = path
    return path


def provider_key_preview(record: CredentialRecord | None) -> str:
    if not record or not record.token:
        return ""
    token = record.token
    if len(token) <= 8:
        return "••••"
    return f"{token[:3]}…{token[-4:]}"


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# OpenRouter OAuth PKCE
# ---------------------------------------------------------------------------


class OpenRouterOAuth:
    """Official OpenRouter PKCE flow with a localhost callback or manual code."""

    def __init__(self, data_dir: str | Path) -> None:
        self.directory = Path(data_dir) / "connections" / "providers"
        self.pending_path = self.directory / OAUTH_PENDING_FILE

    def start(self, callback_url: str, *, key_label: str = "JobForge") -> str:
        verifier = _base64url(secrets.token_bytes(32))
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        state = secrets.token_urlsafe(32)
        pending = self._read_pending()
        now = int(time.time())
        pending = {
            key: value
            for key, value in pending.items()
            if isinstance(value, dict) and now - int(value.get("created_at") or 0) < OAUTH_TTL_SECONDS
        }
        pending[state] = {
            "verifier": verifier,
            "created_at": now,
            "callback_url": callback_url,
        }
        self._write_pending(pending)
        query = urlencode(
            {
                "callback_url": callback_url,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "key_label": key_label,
                "state": state,
            }
        )
        return f"{OPENROUTER_AUTH_URL}?{query}"

    def finish(self, code: str, state: str) -> str:
        code = (code or "").strip()
        state = (state or "").strip()
        if not code:
            raise ProviderAuthError("OpenRouter did not return an authorization code.")
        pending = self._read_pending()
        matched_state = next(
            (
                key
                for key in pending
                if state
                and len(key) == len(state)
                and secrets.compare_digest(key, state)
            ),
            None,
        )
        record = pending.pop(matched_state, None) if matched_state else None
        try:
            if not record or not isinstance(record, dict):
                raise ProviderAuthError("OpenRouter OAuth state is missing or expired.")
            if int(time.time()) - int(record.get("created_at") or 0) >= OAUTH_TTL_SECONDS:
                raise ProviderAuthError("OpenRouter OAuth state expired. Start again.")
            verifier = str(record.get("verifier") or "")
            if not verifier:
                raise ProviderAuthError("OpenRouter OAuth verifier is missing.")
            try:
                response = requests.post(
                    OPENROUTER_EXCHANGE_URL,
                    json={
                        "code": code,
                        "code_verifier": verifier,
                        "code_challenge_method": "S256",
                    },
                    timeout=30,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise ProviderError(f"Could not exchange the OpenRouter code: {exc}") from None
            if response.status_code >= 400:
                raise ProviderAuthError("OpenRouter rejected the authorization code.")
            try:
                payload = response.json()
            except ValueError:
                raise ProviderError("OpenRouter returned a non-JSON token response.") from None
            key = str((payload or {}).get("key") or "").strip()
            if not key:
                raise ProviderAuthError("OpenRouter did not return an API key.")
            return _validated_key(key)
        finally:
            self._write_pending(pending)

    def _read_pending(self) -> dict[str, Any]:
        try:
            data = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_pending(self, pending: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        fd, temp_name = tempfile.mkstemp(
            prefix=".oauth-", dir=str(self.directory), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(pending, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.pending_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")
