from __future__ import annotations

import json
import stat
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from jobforge import db
from jobforge.agent import providers
from jobforge.agent.providers import (
    OpenRouterOAuth,
    ProviderAuthError,
    ProviderClient,
    ProviderCredentialStore,
    base_url_for,
    get_provider,
    infer_provider,
    persist_llm_config,
    provider_status,
    resolve_provider,
)
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Profile
from jobforge.tailoring.llm import LLMClient
from jobforge.web.app import create_app

TEST_KEY = "test-key-" + "a" * 24


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, *, text: str = "") -> None:
        self._payload = payload if payload is not None else {}
        self.status_code = status_code
        self._text = text
        self.closed = False

    def iter_content(self, chunk_size: int = 16384):
        raw = self._text.encode("utf-8") if self._text else json.dumps(self._payload).encode("utf-8")
        yield raw

    def json(self):
        return self._payload

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.responses = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.handler(method, url, **kwargs)
        self.responses.append(response)
        return response


def _config(tmp_path: Path) -> Config:
    cfg = Config(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        db_path=tmp_path / "data" / "test.sqlite3",
        profile_path=None,
        resume_path=None,
        sources={},
        preferences={},
        llm=LLMConfig(),
        email=EmailConfig(),
        browser=BrowserConfig(),
        server=ServerConfig(),
        config_path=tmp_path / "config.yml",
    )
    cfg.ensure_dirs()
    cfg.config_path.write_text("llm:\n  enabled: false\n", encoding="utf-8")
    db.init_db(cfg.db_path)
    conn = db.connect(cfg.db_path)
    profile_id = db.save_profile(conn, Profile(name="provider-test"))
    db.set_active_profile(conn, profile_id)
    conn.close()
    return cfg


def test_provider_aliases_and_auth_truth_table():
    assert get_provider("chatgpt").id == "openai"
    assert get_provider("claude").id == "anthropic"
    assert get_provider("google").id == "gemini"
    assert get_provider("openrouter").supports_oauth is True
    assert get_provider("openai").supports_oauth is False
    assert get_provider("anthropic").supports_oauth is False
    assert get_provider("gemini").supports_oauth is False
    assert infer_provider(LLMConfig(provider="", base_url="http://localhost:11434/v1")) == "ollama"


def test_stale_base_url_falls_back_to_provider_default(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.llm = LLMConfig(
        enabled=True,
        provider="openai",
        base_url="http://localhost:11434/v1",
        model="fake-model",
    )
    assert base_url_for(cfg.llm, get_provider("openai")) == "https://api.openai.com/v1"
    custom = LLMConfig(
        enabled=True,
        provider="custom",
        base_url="https://gateway.internal/v1",
        model="model",
    )
    assert base_url_for(custom, get_provider("custom")) == "https://gateway.internal/v1"


def test_resolve_provider_requires_enabled(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.llm = LLMConfig(
        enabled=False,
        provider="openai",
        base_url="https://api.openai.com/v1",
        model="fake-model",
    )
    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    conn = db.connect(cfg.db_path)
    with pytest.raises(ProviderAuthError):
        resolve_provider(cfg, conn, store=ProviderCredentialStore(tmp_path, keyring_module=False))


def test_credential_store_file_0600_and_env(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    store = ProviderCredentialStore(tmp_path, keyring_module=False)
    record = store.save("openai", TEST_KEY)
    assert record.backend == "file"
    path = tmp_path / "connections" / "providers" / "openai.key"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = store.load("openai")
    assert loaded is not None and loaded.token == TEST_KEY
    store.delete("openai")
    assert store.load("openai") is None

    monkeypatch.setenv("OPENAI_API_KEY", TEST_KEY)
    env_record = store.load("openai")
    assert env_record is not None and env_record.backend == "env"
    assert not path.exists()


def test_openai_models_and_tool_call_parse():
    def handler(method, url, **kwargs):
        if url.endswith("/models"):
            return FakeResponse({"data": [{"id": "model-a"}, {"id": "model-b"}]})
        return FakeResponse(
            {
                "model": "model-a",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "search_jobs",
                                        "arguments": '{"query": "Acme"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
            }
        )

    session = FakeSession(handler)
    client = ProviderClient(
        get_provider("openai"),
        base_url="https://api.example.test/v1",
        api_key=TEST_KEY,
        model="model-a",
        session=session,
    )
    assert client.list_models() == ["model-a", "model-b"]
    result = client.chat([{"role": "user", "content": "find"}], tools=[{"type": "function"}])
    assert result.tool_calls == [
        {"id": "call_1", "name": "search_jobs", "arguments": {"query": "Acme"}}
    ]
    assert result.usage["total_tokens"] == 16
    assert session.calls[1]["allow_redirects"] is False
    assert "Authorization" in session.calls[1]["headers"]


def test_anthropic_messages_and_tool_parse():
    captured: dict = {}

    def handler(method, url, **kwargs):
        captured.update(kwargs)
        return FakeResponse(
            {
                "model": "claude-test",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {"type": "tool_use", "id": "toolu_1", "name": "explain_fit", "input": {"job_id": 3}},
                ],
                "usage": {"input_tokens": 20, "output_tokens": 8},
                "stop_reason": "tool_use",
            }
        )

    client = ProviderClient(
        get_provider("anthropic"),
        base_url="https://api.anthropic.test",
        api_key=TEST_KEY,
        model="claude-test",
        session=FakeSession(handler),
    )
    result = client.chat(
        [
            {"role": "system", "content": "Safety rules"},
            {"role": "user", "content": "Explain job 3"},
        ],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "explain_fit",
                    "description": "Explain",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )
    assert captured["json"]["system"] == "Safety rules"
    assert captured["json"]["tools"][0]["input_schema"] == {"type": "object"}
    assert result.content == "Checking."
    assert result.tool_calls == [
        {"id": "toolu_1", "name": "explain_fit", "arguments": {"job_id": 3}}
    ]
    assert result.usage["total_tokens"] == 28


def test_anthropic_tool_result_conversion():
    system, converted = providers._anthropic_messages(
        [
            {"role": "system", "content": "rules"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "toolu_1", "name": "get_job", "arguments": {"job_id": 3}}],
            },
            {"role": "tool", "tool_call_id": "toolu_1", "name": "get_job", "content": "{\"ok\": true}"},
        ]
    )
    assert system == "rules"
    assert converted[0]["content"][0]["type"] == "tool_use"
    assert converted[1]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_1",
        "content": '{"ok": true}',
    }


def test_gemini_model_list_filters_embeddings():
    def handler(method, url, **kwargs):
        return FakeResponse(
            {
                "models": [
                    {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                    {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
                ]
            }
        )

    client = ProviderClient(
        get_provider("gemini"),
        base_url=get_provider("gemini").default_base_url,
        api_key=TEST_KEY,
        model="gemini-2.5-flash",
        session=FakeSession(handler),
    )
    assert client.list_models() == ["gemini-2.5-flash"]


def test_provider_error_redacts_key():
    def handler(method, url, **kwargs):
        return FakeResponse({"error": {"message": f"bad key {TEST_KEY}"}}, status_code=400)

    client = ProviderClient(
        get_provider("openai"),
        base_url="https://api.example.test/v1",
        api_key=TEST_KEY,
        model="model-a",
        session=FakeSession(handler),
    )
    with pytest.raises(providers.ProviderModelError) as exc:
        client.chat([{"role": "user", "content": "hi"}])
    assert TEST_KEY not in str(exc.value)
    assert "[redacted]" in str(exc.value)


def test_provider_redirect_is_rejected():
    def handler(method, url, **kwargs):
        return FakeResponse({"error": "moved"}, status_code=307)

    client = ProviderClient(
        get_provider("openai"),
        base_url="https://api.example.test/v1",
        api_key=TEST_KEY,
        model="model-a",
        session=FakeSession(handler),
    )
    with pytest.raises(providers.ProviderError) as exc:
        client.chat([{"role": "user", "content": "hi"}])
    assert "redirect" in str(exc.value).lower()
    assert TEST_KEY not in str(exc.value)


def test_openrouter_oauth_pkce_and_state(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        providers.requests,
        "post",
        lambda *args, **kwargs: FakeResponse({"key": TEST_KEY}),
    )
    oauth = OpenRouterOAuth(tmp_path)
    url = oauth.start("http://127.0.0.1:8765/providers/oauth/openrouter/callback")
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "openrouter.ai"
    assert params["code_challenge_method"] == ["S256"]
    state = params["state"][0]
    assert oauth.finish("auth-code", state) == TEST_KEY
    with pytest.raises(ProviderAuthError):
        oauth.finish("auth-code", state)
    with pytest.raises(ProviderAuthError):
        oauth.finish("auth-code", "wrong-state")


def test_provider_connect_activate_and_no_secret_leak(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JOBFORGE_DISABLE_KEYRING", "1")
    cfg = _config(tmp_path)
    client = TestClient(create_app(cfg))

    response = client.post(
        "/providers/openai/connect",
        data={"key": TEST_KEY, "base_url": "https://api.openai.com/v1", "model": "fake-model"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    response = client.post(
        "/providers/active",
        data={
            "provider_id": "openai",
            "model": "fake-model",
            "base_url": "https://api.openai.com/v1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    config_text = Path(cfg.config_path).read_text(encoding="utf-8")
    assert TEST_KEY not in config_text
    assert "fake-model" in config_text
    page = client.get("/providers")
    assert page.status_code == 200
    assert TEST_KEY not in page.text
    conn = db.connect(cfg.db_path)
    assert TEST_KEY not in json.dumps(db.export_all(conn))
    connection = db.get_connection(conn, "llm:openai")
    assert connection is not None
    assert connection["token_backend"] == "file"
    assert (tmp_path / "data" / "connections" / "providers" / "openai.key").stat().st_mode & 0o777 == 0o600


def test_openrouter_callback_does_not_leak_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JOBFORGE_DISABLE_KEYRING", "1")
    cfg = _config(tmp_path)
    monkeypatch.setattr(OpenRouterOAuth, "finish", lambda self, code, state: TEST_KEY)
    client = TestClient(create_app(cfg))
    response = client.get(
        "/providers/oauth/openrouter/callback?code=auth-code&state=ok",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert TEST_KEY not in response.headers.get("location", "")
    page = client.get("/providers")
    assert TEST_KEY not in page.text
    conn = db.connect(cfg.db_path)
    assert TEST_KEY not in json.dumps(db.export_all(conn))


def test_openrouter_callback_is_reachable_with_server_token(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JOBFORGE_DISABLE_KEYRING", "1")
    cfg = _config(tmp_path)
    cfg.server = ServerConfig(access_token="local-test-token")
    monkeypatch.setattr(OpenRouterOAuth, "finish", lambda self, code, state: TEST_KEY)
    client = TestClient(create_app(cfg))
    response = client.get(
        "/providers/oauth/openrouter/callback?code=auth-code&state=ok",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert TEST_KEY not in response.headers.get("location", "")
    assert client.get("/providers").status_code == 401


def test_credential_bearing_base_url_is_rejected(tmp_path: Path):
    cfg = _config(tmp_path)
    before = Path(cfg.config_path).read_text(encoding="utf-8")
    with pytest.raises(providers.ProviderError):
        persist_llm_config(
            cfg,
            provider="openai",
            model="model",
            base_url="https://user:pass@api.example.test/v1",
        )
    assert Path(cfg.config_path).read_text(encoding="utf-8") == before
    assert "user:pass" not in Path(cfg.config_path).read_text(encoding="utf-8")


def test_persist_llm_config_removes_legacy_key(tmp_path: Path):
    cfg = _config(tmp_path)
    cfg.config_path.write_text(
        "llm:\n  enabled: true\n  api_key: legacy-secret-value\n  model: old-model\n",
        encoding="utf-8",
    )
    persist_llm_config(
        cfg,
        provider="openai",
        model="new-model",
        base_url="https://api.openai.com/v1",
    )
    text = Path(cfg.config_path).read_text(encoding="utf-8")
    assert "legacy-secret-value" not in text
    assert "api_key" not in text
    assert "new-model" in text


def _fake_claude_run(result_text: str = "Hello from Claude Code"):
    calls = []

    def run(command, **kwargs):
        calls.append({"command": command, **kwargs})
        if command[1:] == ["--version"]:
            return SimpleNamespace(returncode=0, stdout="2.1.310\n", stderr="")
        if command[1:3] == ["auth", "status"]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "result": result_text,
                    "is_error": False,
                    "usage": {"input_tokens": 12, "output_tokens": 4},
                }
            ),
            stderr="",
        )

    return run, calls


def test_claude_code_status_and_client_use_official_cli(tmp_path: Path, monkeypatch):
    run, calls = _fake_claude_run("Synthesized answer.")
    monkeypatch.setattr(providers.shutil, "which", lambda name: "/fake/claude")
    monkeypatch.setattr(providers.subprocess, "run", run)

    status = providers.claude_code_status()
    assert status.available is True
    assert status.authenticated is True
    assert status.auth_method == "claude.ai"

    client = providers.ClaudeCodeClient(
        get_provider("claude_code"), model="sonnet", timeout=60
    )
    assert client.supports_tools is False
    result = client.chat(
        [
            {"role": "system", "content": "RULES\n\nCANDIDATE CONTEXT:\nprivate context"},
            {"role": "user", "content": "Find my matches"},
        ],
        tools=[{"type": "function", "function": {"name": "search_jobs"}}],
    )
    assert result.content == "Synthesized answer."
    assert result.tool_calls == []
    assert result.usage == {"input_tokens": 12, "output_tokens": 4, "total_tokens": 16}

    chat_call = next(call for call in calls if call["command"][1] == "-p")
    command = chat_call["command"]
    assert "--bare" not in command
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in command
    assert "--no-session-persistence" in command
    system_prompt = command[command.index("--append-system-prompt") + 1]
    assert "RULES" in system_prompt
    assert "private context" not in system_prompt
    assert "private context" in chat_call["input"]
    assert "Find my matches" in chat_call["input"]


def test_claude_code_status_missing_and_signed_out(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", lambda name: "")
    missing = providers.claude_code_status()
    assert missing.available is False
    assert missing.authenticated is False

    monkeypatch.setattr(providers.shutil, "which", lambda name: "/fake/claude")
    monkeypatch.setattr(
        providers.subprocess,
        "run",
        lambda command, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
            stderr="",
        ),
    )
    signed_out = providers.claude_code_status()
    assert signed_out.available is True
    assert signed_out.authenticated is False


def test_resolve_provider_claude_code_requires_sign_in(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.llm = LLMConfig(enabled=True, provider="claude_code", model="sonnet")
    conn = db.connect(cfg.db_path)
    store = ProviderCredentialStore(tmp_path, keyring_module=False)

    monkeypatch.setattr(
        providers,
        "claude_code_status",
        lambda: providers.ClaudeCodeStatus(
            available=True, authenticated=True, auth_method="claude.ai", version="2.1.310"
        ),
    )
    resolved = resolve_provider(cfg, conn, store=store)
    assert isinstance(resolved.client, providers.ClaudeCodeClient)
    assert resolved.backend == "cli"
    assert resolved.base_url == ""
    assert resolved.model == "sonnet"

    monkeypatch.setattr(
        providers,
        "claude_code_status",
        lambda: providers.ClaudeCodeStatus(
            available=True, authenticated=False, error="Run claude auth login."
        ),
    )
    with pytest.raises(ProviderAuthError):
        resolve_provider(cfg, conn, store=store)


def test_provider_status_and_page_for_claude_code(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JOBFORGE_DISABLE_KEYRING", "1")
    cfg = _config(tmp_path)
    conn = db.connect(cfg.db_path)
    monkeypatch.setattr(
        providers,
        "claude_code_status",
        lambda: providers.ClaudeCodeStatus(
            available=True, authenticated=True, auth_method="claude.ai", version="2.1.310"
        ),
    )
    cards = provider_status(cfg, conn, store=ProviderCredentialStore(tmp_path, keyring_module=False))
    card = next(item for item in cards if item["spec"].id == "claude_code")
    assert card["connected"] is True
    assert card["backend"] == "cli"
    assert card["cli"]["authenticated"] is True
    assert "sonnet" in card["models"]

    client = TestClient(create_app(cfg))
    page = client.get("/providers")
    assert page.status_code == 200
    assert "Claude Code (subscription CLI)" in page.text
    assert "never reads" in page.text
    assert "Check Claude Code sign-in" in page.text

    response = client.post(
        "/providers/claude_code/connect",
        data={"model": "sonnet"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    connection = db.get_connection(conn, "llm:claude_code")
    assert connection is not None
    assert connection["token_backend"] == "cli"


def test_llm_client_uses_claude_code_bridge(tmp_path: Path, monkeypatch):
    cfg = _config(tmp_path)
    cfg.llm = LLMConfig(enabled=True, provider="claude_code", model="sonnet")
    monkeypatch.setattr(
        "jobforge.tailoring.llm.claude_code_status",
        lambda: providers.ClaudeCodeStatus(
            available=True, authenticated=True, auth_method="claude.ai"
        ),
    )
    monkeypatch.setattr(
        providers.ClaudeCodeClient,
        "chat",
        lambda self, messages, **kwargs: providers.ChatResult(content="Bridge OK"),
    )
    client = LLMClient(cfg)
    assert client.available is True
    assert client.chat("system", "user") == "Bridge OK"


def test_claude_code_page_warns_when_using_env_api_key(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JOBFORGE_DISABLE_KEYRING", "1")
    cfg = _config(tmp_path)
    monkeypatch.setattr(
        providers,
        "claude_code_status",
        lambda: providers.ClaudeCodeStatus(
            available=True, authenticated=True, auth_method="api_key", version="2.1.310"
        ),
    )
    client = TestClient(create_app(cfg))
    page = client.get("/providers")
    assert page.status_code == 200
    assert "not your Claude subscription" in page.text
    assert "ANTHROPIC_API_KEY" in page.text
