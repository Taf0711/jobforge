"""Configuration loading.

Secrets are never stored in config.yml. Use environment variables for the
LLM API key and the IMAP password.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import base_dir, default_config_path, example_config


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get(data: dict[str, Any], path: str, default: Any = None) -> Any:
    cur: Any = data
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _resolve(value: str | None) -> Path | None:
    if not value:
        return None
    p = Path(value).expanduser()
    return p if p.is_absolute() else (base_dir() / p)


@dataclass
class LLMConfig:
    enabled: bool = False
    provider: str = ""
    base_url: str = "http://localhost:11434/v1"
    api_key: str = ""
    model: str = "llama3.1:8b"
    timeout: int = 120
    temperature: float = 0.2
    max_tokens: int = 4096

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.base_url and self.model)


@dataclass
class TailoringConfig:
    mode: str = "auto"  # auto | llm | deterministic

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode}


@dataclass
class AgentConfig:
    max_steps: int = 8
    max_seconds: int = 300
    max_tool_calls: int = 6
    allow_writes: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_seconds": self.max_seconds,
            "max_tool_calls": self.max_tool_calls,
            "allow_writes": self.allow_writes,
        }


@dataclass
class EmailConfig:
    enabled: bool = False
    host: str = "imap.gmail.com"
    port: int = 993
    username: str = ""
    password_env: str = "JOBFORGE_IMAP_PASSWORD"
    folder: str = "INBOX"
    lookback_days: int = 30
    verification_domains: list[str] = field(default_factory=list)
    verification_senders: list[str] = field(default_factory=list)

    @property
    def password(self) -> str:
        return _env(self.password_env)


@dataclass
class BrowserConfig:
    headless: bool = False
    user_data_dir: str = "data/browser"
    slow_mo: int = 0
    timeout_ms: int = 30_000
    allow_submit: bool = False
    channel: str = ""  # "chrome" uses the system Chrome instead of bundled Chromium


@dataclass
class FeatureFlags:
    setup_wizard: bool = True
    github_connections: bool = True
    scoring_v2: bool = True
    agent: bool = True
    agent_integrations: bool = True

    def to_dict(self) -> dict[str, bool]:
        return {
            "setup_wizard": self.setup_wizard,
            "github_connections": self.github_connections,
            "scoring_v2": self.scoring_v2,
            "agent": self.agent,
            "agent_integrations": self.agent_integrations,
        }


FEATURE_ENV = {
    "setup_wizard": "JOBFORGE_FEATURE_SETUP_WIZARD",
    "github_connections": "JOBFORGE_FEATURE_GITHUB_CONNECTIONS",
    "scoring_v2": "JOBFORGE_FEATURE_SCORING_V2",
    "agent": "JOBFORGE_FEATURE_AGENT",
    "agent_integrations": "JOBFORGE_FEATURE_AGENT_INTEGRATIONS",
}


def _feature(data: dict[str, Any], name: str, default: bool) -> bool:
    raw = os.environ.get(FEATURE_ENV[name], "").strip()
    if not raw:
        raw = _get(data, f"features.{name}", default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return bool(raw)


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    access_token: str = ""

    @property
    def token(self) -> str:
        return self.access_token or _env("JOBFORGE_TOKEN")


@dataclass
class Config:
    data_dir: Path
    output_dir: Path
    db_path: Path
    profile_path: Path | None
    resume_path: Path | None
    sources: dict[str, Any]
    preferences: dict[str, Any]
    llm: LLMConfig
    email: EmailConfig
    browser: BrowserConfig
    server: ServerConfig
    config_path: Path | None = None
    scan: dict[str, Any] = field(default_factory=dict)
    features: FeatureFlags = field(default_factory=FeatureFlags)
    agent: AgentConfig = field(default_factory=AgentConfig)
    tailoring: TailoringConfig = field(default_factory=TailoringConfig)

    def ensure_dirs(self) -> None:
        for p in (
            self.data_dir,
            self.output_dir,
            self.data_dir / "uploads",
            self.data_dir / "screenshots",
            self.data_dir / "connections" / "providers",
        ):
            p.mkdir(parents=True, exist_ok=True)


def _defaults() -> dict[str, Any]:
    example = example_config()
    if example.exists():
        return yaml.safe_load(example.read_text(encoding="utf-8")) or {}
    return {}


def _tailoring_mode(data: dict[str, Any]) -> str:
    raw = (
        _env("JOBFORGE_TAILORING_MODE")
        or _get(data, "tailoring.mode", "auto")
        or "auto"
    )
    mode = str(raw).strip().lower()
    return mode if mode in {"auto", "llm", "deterministic"} else "auto"


def load_config(path: str | Path | None = None) -> Config:
    """Load config.yml (or the example defaults) and apply env overrides."""
    raw_path = path or default_config_path()
    config_path = Path(raw_path).expanduser()
    if config_path.exists():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    else:
        data = _defaults()
        config_path = None

    # Environment overrides for paths and local-only settings.
    data_dir = _resolve(_env("JOBFORGE_DATA_DIR") or _get(data, "data_dir", "data")) or (base_dir() / "data")
    output_dir = _resolve(_env("JOBFORGE_OUTPUT_DIR") or _get(data, "output_dir", "output")) or (base_dir() / "output")
    db_path = _resolve(_env("JOBFORGE_DB")) or (data_dir / "jobforge.sqlite3")

    llm = LLMConfig(
        enabled=bool(_get(data, "llm.enabled", False)),
        provider=_env("JOBFORGE_LLM_PROVIDER") or _get(data, "llm.provider", ""),
        base_url=_env("JOBFORGE_LLM_BASE_URL") or _get(data, "llm.base_url", "http://localhost:11434/v1"),
        api_key=_env("JOBFORGE_LLM_API_KEY") or _get(data, "llm.api_key", ""),
        model=_env("JOBFORGE_LLM_MODEL") or _get(data, "llm.model", "llama3.1:8b"),
        timeout=int(_get(data, "llm.timeout", 120)),
        temperature=float(_get(data, "llm.temperature", 0.2)),
        max_tokens=int(_env("JOBFORGE_LLM_MAX_TOKENS") or _get(data, "llm.max_tokens", 4096)),
    )

    email = EmailConfig(
        enabled=bool(_get(data, "email.enabled", False)),
        host=_env("JOBFORGE_IMAP_HOST") or _get(data, "email.host", "imap.gmail.com"),
        port=int(_env("JOBFORGE_IMAP_PORT") or _get(data, "email.port", 993)),
        username=_env("JOBFORGE_IMAP_USER") or _get(data, "email.username", ""),
        password_env=_get(data, "email.password_env", "JOBFORGE_IMAP_PASSWORD"),
        folder=_get(data, "email.folder", "INBOX"),
        lookback_days=int(_get(data, "email.lookback_days", 30)),
        verification_domains=list(_get(data, "email.verification_domains", []) or []),
        verification_senders=list(_get(data, "email.verification_senders", []) or []),
    )

    browser_user_data = _resolve(
        _env("JOBFORGE_BROWSER_DIR") or _get(data, "browser.user_data_dir", "data/browser")
    ) or (base_dir() / "data" / "browser")
    browser = BrowserConfig(
        headless=bool(_get(data, "browser.headless", False)),
        user_data_dir=str(browser_user_data),
        slow_mo=int(_get(data, "browser.slow_mo", 0)),
        timeout_ms=int(_get(data, "browser.timeout_ms", 30_000)),
        allow_submit=bool(_get(data, "browser.allow_submit", False)),
        channel=str(_get(data, "browser.channel", "") or ""),
    )

    server = ServerConfig(
        host=_env("JOBFORGE_HOST") or _get(data, "server.host", "127.0.0.1"),
        port=int(_env("JOBFORGE_PORT") or _get(data, "server.port", 8765)),
        access_token=_env("JOBFORGE_TOKEN") or _get(data, "server.access_token", ""),
    )

    cfg = Config(
        data_dir=data_dir,
        output_dir=output_dir,
        db_path=db_path,
        profile_path=_resolve(_get(data, "profile")),
        resume_path=_resolve(_get(data, "resume")),
        sources=dict(_get(data, "sources", {}) or {}),
        preferences=dict(_get(data, "preferences", {}) or {}),
        llm=llm,
        email=email,
        browser=browser,
        server=server,
        config_path=config_path,
        scan=dict(_get(data, "scan", {}) or {}),
        features=FeatureFlags(
            setup_wizard=_feature(data, "setup_wizard", True),
            github_connections=_feature(data, "github_connections", True),
            scoring_v2=_feature(data, "scoring_v2", True),
            agent=_feature(data, "agent", True),
            agent_integrations=_feature(data, "agent_integrations", True),
        ),
        tailoring=TailoringConfig(
            mode=_tailoring_mode(data),
        ),
        agent=AgentConfig(
            max_steps=max(
                1, min(32, int(_env("JOBFORGE_AGENT_MAX_STEPS") or _get(data, "agent.max_steps", 8)))
            ),
            max_seconds=max(
                30,
                min(
                    3600,
                    int(_env("JOBFORGE_AGENT_MAX_SECONDS") or _get(data, "agent.max_seconds", 300)),
                ),
            ),
            max_tool_calls=max(
                1,
                min(
                    64,
                    int(
                        _env("JOBFORGE_AGENT_MAX_TOOL_CALLS")
                        or _get(data, "agent.max_tool_calls", 6)
                    ),
                ),
            ),
            allow_writes=bool(_get(data, "agent.allow_writes", False)),
        ),
    )
    cfg.ensure_dirs()
    return cfg
