"""Model-provider setup and model selection routes."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from functools import wraps
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db, services
from ..agent.providers import (
    OpenRouterOAuth,
    ProviderAuthError,
    ProviderCredentialStore,
    ProviderError,
    claude_code_status,
    get_provider,
    infer_provider,
    make_provider_client,
    persist_llm_config,
    provider_status,
    remember_provider_connection,
)
from ..config import Config
from ..locking import action_lock

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["short"] = lambda v, n=140: (str(v or "")[:n] + ("..." if len(str(v or "")) > n else ""))


def _msg(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def _safe_error(exc: Exception) -> str:
    return (str(exc).strip() or exc.__class__.__name__)[:300]


def _store(config: Config) -> ProviderCredentialStore:
    if os.environ.get("JOBFORGE_DISABLE_KEYRING") == "1":
        return ProviderCredentialStore(config.data_dir, keyring_module=False)
    return ProviderCredentialStore(config.data_dir)


def create_provider_router(config: Config) -> APIRouter:
    router = APIRouter()

    def get_conn() -> Generator[sqlite3.Connection, None, None]:
        conn = db.connect(config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def locked_write(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with action_lock(config, blocking=False):
                return fn(*args, **kwargs)

        return wrapper

    def page_context(request: Request, conn: sqlite3.Connection) -> dict:
        profile, prefs = services.require_profile(config, conn)
        cards = provider_status(config, conn, store=_store(config))
        active_provider = infer_provider(config.llm)
        active_card = next(
            (card for card in cards if card["spec"].id == active_provider), None
        )
        return {
            "request": request,
            "profile": profile,
            "prefs": prefs,
            "cfg": config,
            "features": config.features,
            "msg": request.query_params.get("msg", ""),
            "cards": cards,
            "active_provider": active_provider,
            "active_model": config.llm.model,
            "active_card": active_card,
            "llm_enabled": config.llm.enabled,
        }

    @router.get("/providers", response_class=HTMLResponse)
    def providers_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        return templates.TemplateResponse(
            request=request,
            name="providers.html",
            context=page_context(request, conn),
        )

    @router.post("/providers/{provider_id}/connect")
    @locked_write
    def connect(
        provider_id: str,
        key: str = Form(""),
        base_url: str = Form(""),
        model: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            spec = get_provider(provider_id)
            store = _store(config)
            if spec.auth_mode == "cli":
                status = claude_code_status()
                if not status.authenticated:
                    raise ProviderAuthError(
                        status.error
                        or "Claude Code is not signed in. Run `claude auth login` in a terminal."
                    )
                remember_provider_connection(
                    conn,
                    spec,
                    backend="cli",
                    base_url="",
                    models=list(spec.fallback_models),
                    model=model.strip(),
                    display_name=spec.label,
                )
                method = status.auth_method or "signed in"
                return RedirectResponse(
                    f"/providers?msg={_msg(f'Claude Code detected ({method}). JobForge does not read or store its credential.')}",
                    status_code=303,
                )
            resolved_base = (base_url or spec.default_base_url).strip()
            if not resolved_base:
                raise ProviderAuthError("A custom provider needs a base URL.")
            record = store.load(spec.id) if not key.strip() else store.save(spec.id, key)
            if record is None and spec.auth_mode not in {"local", "optional"}:
                raise ProviderAuthError(
                    "Enter an API key, or set the provider environment variable."
                )
            backend = record.backend if record else "local"
            remember_provider_connection(
                conn,
                spec,
                backend=backend,
                base_url=resolved_base,
                models=list(spec.fallback_models),
                model=model.strip(),
                display_name=spec.label,
            )
            warning = f" {record.warning}" if record and record.warning else ""
            return RedirectResponse(
                f"/providers?msg={_msg(f'{spec.label} connected.{warning}')}",
                status_code=303,
            )
        except (ProviderError, OSError) as exc:
            return RedirectResponse(
                f"/providers?msg={_msg(f'Connection failed: {_safe_error(exc)}')}",
                status_code=303,
            )

    @router.post("/providers/{provider_id}/disconnect")
    @locked_write
    def disconnect(provider_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            spec = get_provider(provider_id)
            _store(config).delete(spec.id)
            db.delete_connection(conn, f"llm:{spec.id}")
            db.log_event(conn, None, "provider_disconnected", spec.id)
            if spec.auth_mode == "cli":
                message = (
                    f"{spec.label} metadata removed. Claude Code sign-in is managed by "
                    "Claude Code itself; JobForge never stored it."
                )
            else:
                message = f"{spec.label} disconnected. Stored metadata was removed."
            return RedirectResponse(f"/providers?msg={_msg(message)}", status_code=303)
        except (ProviderError, OSError) as exc:
            return RedirectResponse(
                f"/providers?msg={_msg(_safe_error(exc))}", status_code=303
            )

    @router.post("/providers/{provider_id}/refresh")
    @locked_write
    def refresh_models(provider_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            spec = get_provider(provider_id)
        except ProviderError as exc:
            return RedirectResponse(
                f"/providers?msg={_msg(_safe_error(exc))}", status_code=303
            )
        connection = db.get_connection(conn, f"llm:{spec.id}")
        meta = (connection or {}).get("meta") or {}
        store = _store(config)
        record = store.load(spec.id, str((connection or {}).get("token_backend") or ""))
        if record is None and spec.auth_mode not in {"local", "optional", "cli"}:
            return RedirectResponse(
                f"/providers?msg={_msg('Connect the provider before refreshing models.')}",
                status_code=303,
            )
        if spec.auth_mode == "cli":
            status = claude_code_status()
            if not status.authenticated:
                return RedirectResponse(
                    f"/providers?msg={_msg(status.error or 'Claude Code is not signed in.')}",
                    status_code=303,
                )
            models = list(spec.fallback_models)
            remember_provider_connection(
                conn,
                spec,
                backend="cli",
                base_url="",
                models=models,
                model=str(meta.get("model") or ""),
                display_name=spec.label,
            )
            aliases = ", ".join(models)
            return RedirectResponse(
                f"/providers?msg={_msg(f'Claude Code model aliases: {aliases}.')}",
                status_code=303,
            )
        try:
            client = make_provider_client(
                spec,
                base_url=str(meta.get("base_url") or spec.default_base_url),
                api_key=record.token if record else "",
                model=str(
                    meta.get("model")
                    or (spec.fallback_models[0] if spec.fallback_models else "model")
                ),
                timeout=config.llm.timeout,
                temperature=config.llm.temperature,
                max_tokens=config.llm.max_tokens,
            )
            models = client.list_models()
            remember_provider_connection(
                conn,
                spec,
                backend=record.backend if record else "local",
                base_url=str(meta.get("base_url") or spec.default_base_url),
                models=models,
                model=str(meta.get("model") or ""),
                display_name=spec.label,
            )
            return RedirectResponse(
                f"/providers?msg={_msg(f'Found {len(models)} model(s) for {spec.label}.')}",
                status_code=303,
            )
        except (ProviderError, OSError) as exc:
            return RedirectResponse(
                f"/providers?msg={_msg(f'Model refresh failed: {_safe_error(exc)}')}",
                status_code=303,
            )

    @router.post("/providers/active")
    @locked_write
    def activate(
        provider_id: str = Form(...),
        model: str = Form(...),
        base_url: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            spec = get_provider(provider_id)
        except ProviderError as exc:
            return RedirectResponse(
                f"/providers?msg={_msg(_safe_error(exc))}", status_code=303
            )
        model = model.strip()
        if not model:
            return RedirectResponse(
                f"/providers?msg={_msg('Enter or choose a model first.')}",
                status_code=303,
            )
        store = _store(config)
        record = store.load(spec.id)
        if record is None and spec.auth_mode not in {"local", "optional", "cli"}:
            return RedirectResponse(
                f"/providers?msg={_msg('Connect the provider before selecting a model.')}",
                status_code=303,
            )
        if spec.auth_mode == "cli":
            status = claude_code_status()
            if not status.authenticated:
                return RedirectResponse(
                    f"/providers?msg={_msg(status.error or 'Claude Code is not signed in.')}",
                    status_code=303,
                )
        resolved_base = (base_url or spec.default_base_url).strip()
        try:
            persist_llm_config(
                config,
                provider=spec.id,
                model=model,
                base_url=resolved_base,
                enabled=True,
            )
            remember_provider_connection(
                conn,
                spec,
                backend=record.backend if record else "local",
                base_url=resolved_base,
                model=model,
                display_name=spec.label,
            )
            db.log_event(conn, None, "provider_active", f"{spec.id}:{model}")
            return RedirectResponse(
                f"/providers?msg={_msg(f'Active model set to {spec.label} · {model}.')}",
                status_code=303,
            )
        except (ProviderError, OSError, ValueError) as exc:
            return RedirectResponse(
                f"/providers?msg={_msg(f'Could not save the model: {_safe_error(exc)}')}",
                status_code=303,
            )

    @router.post("/providers/{provider_id}/test")
    def test_provider(provider_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            spec = get_provider(provider_id)
        except ProviderError as exc:
            return RedirectResponse(
                f"/providers?msg={_msg(_safe_error(exc))}", status_code=303
            )
        connection = db.get_connection(conn, f"llm:{spec.id}")
        meta = (connection or {}).get("meta") or {}
        store = _store(config)
        record = store.load(spec.id, str((connection or {}).get("token_backend") or ""))
        if record is None and spec.auth_mode not in {"local", "optional", "cli"}:
            return RedirectResponse(
                f"/providers?msg={_msg('Connect the provider before testing it.')}",
                status_code=303,
            )
        if spec.auth_mode == "cli":
            status = claude_code_status()
            if not status.authenticated:
                return RedirectResponse(
                    f"/providers?msg={_msg(status.error or 'Claude Code is not signed in.')}",
                    status_code=303,
                )
        model = (
            str(meta.get("model") or spec.fallback_models[0])
            if spec.auth_mode == "cli" and spec.fallback_models
            else str(
                meta.get("model")
                or config.llm.model
                or (spec.fallback_models[0] if spec.fallback_models else "")
            )
        )
        if not model:
            return RedirectResponse(
                f"/providers?msg={_msg('Choose a model before testing the provider.')}",
                status_code=303,
            )
        try:
            client = make_provider_client(
                spec,
                base_url=str(meta.get("base_url") or spec.default_base_url),
                api_key=record.token if record else "",
                model=model,
                timeout=config.llm.timeout,
                temperature=config.llm.temperature,
                max_tokens=config.llm.max_tokens,
            )
            result = client.test_chat()
            reply = (result.content or "").strip().replace("\n", " ")[:120]
            if record and record.token:
                reply = reply.replace(record.token, "[redacted]")
            empty_marker = reply or "(empty)"
            return RedirectResponse(
                f"/providers?msg={_msg(f'{spec.label} replied with {model}: {empty_marker}')}",
                status_code=303,
            )
        except (ProviderError, OSError) as exc:
            return RedirectResponse(
                f"/providers?msg={_msg(f'Test failed: {_safe_error(exc)}')}",
                status_code=303,
            )

    @router.get("/providers/oauth/openrouter/start")
    @locked_write
    def openrouter_start(request: Request):
        callback = str(request.base_url).rstrip("/") + "/providers/oauth/openrouter/callback"
        try:
            url = OpenRouterOAuth(config.data_dir).start(callback)
        except Exception as exc:  # noqa: BLE001
            return RedirectResponse(
                f"/providers?msg={_msg(f'Could not start OpenRouter sign-in: {_safe_error(exc)}')}",
                status_code=303,
            )
        return RedirectResponse(url, status_code=303)

    @router.get("/providers/oauth/openrouter/callback")
    @locked_write
    def openrouter_callback(
        request: Request,
        code: str = "",
        state: str = "",
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            key = OpenRouterOAuth(config.data_dir).finish(code, state)
            spec = get_provider("openrouter")
            record = _store(config).save(spec.id, key)
            remember_provider_connection(
                conn,
                spec,
                backend=record.backend,
                base_url=spec.default_base_url,
                models=list(spec.fallback_models),
                model="",
                display_name=spec.label,
            )
            db.log_event(conn, None, "provider_connected", "openrouter:oauth")
            return RedirectResponse(
                f"/providers?msg={_msg('OpenRouter connected with OAuth. Refresh models to choose one.')}",
                status_code=303,
            )
        except (ProviderError, OSError) as exc:
            return RedirectResponse(
                f"/providers?msg={_msg(f'OpenRouter sign-in failed: {_safe_error(exc)}')}",
                status_code=303,
            )

    return router
