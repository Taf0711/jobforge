"""Agent chat and conversation routes."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from .. import db, services
from ..agent.providers import (
    ProviderAuthError,
    ProviderCredentialStore,
    ProviderError,
    infer_provider,
    provider_status,
)
from ..agent.runtime import AgentError, AgentRuntime
from ..config import Config

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["short"] = lambda v, n=140: (str(v or "")[:n] + ("..." if len(str(v or "")) > n else ""))

SUGGESTIONS = [
    "Find my best five matches and explain the gaps.",
    "What skills and projects should I emphasize for my top match?",
    "Review my profile for evidence and portfolio gaps.",
    "Which companies have a warm path I should use first?",
    "Tailor my best match and tell me whether it is ready to fill.",
    "Summarize my application funnel and channel performance.",
]


class AgentMessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=12_000)
    allow_writes: bool = False


def _msg(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def _safe_error(exc: Exception) -> str:
    return (str(exc).strip() or exc.__class__.__name__)[:500]


def create_agent_router(config: Config) -> APIRouter:
    router = APIRouter()

    def get_conn() -> Generator[sqlite3.Connection, None, None]:
        conn = db.connect(config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _provider_state(conn: sqlite3.Connection) -> tuple[bool, str]:
        if not config.llm.enabled:
            return False, ""
        try:
            cards = provider_status(config, conn, store=ProviderCredentialStore(config.data_dir))
        except Exception:  # noqa: BLE001 - status must not break the page
            return False, ""
        active = infer_provider(config.llm)
        card = next((item for item in cards if item["spec"].id == active), None)
        if not card:
            return False, ""
        return bool(card["connected"] and config.llm.model), card["spec"].label

    def _thread_for(conn: sqlite3.Connection, profile_id: int, thread_id: int) -> dict:
        thread = db.get_agent_thread(conn, thread_id)
        if thread is None or int(thread["profile_id"]) != int(profile_id):
            raise HTTPException(status_code=404, detail="Conversation not found")
        return thread

    def _messages(conn: sqlite3.Connection, thread_id: int) -> list[dict]:
        return db.list_agent_messages(conn, thread_id, limit=300)

    @router.get("/agent", response_class=HTMLResponse)
    def agent_page(
        request: Request,
        thread: int | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        profile, prefs = services.require_profile(config, conn)
        threads = db.list_agent_threads(conn, profile.id or 0, limit=50)
        selected = None
        if thread is not None:
            selected = db.get_agent_thread(conn, thread)
            if selected is None or int(selected["profile_id"]) != int(profile.id or 0):
                selected = None
        if selected is None and thread is None and threads:
            # Do not silently select a thread; the welcome state is useful.
            selected = None
        messages = _messages(conn, int(selected["id"])) if selected else []
        ready, provider_label = _provider_state(conn)
        return templates.TemplateResponse(
            request=request,
            name="agent.html",
            context={
                "request": request,
                "profile": profile,
                "prefs": prefs,
                "cfg": config,
                "features": config.features,
                "msg": request.query_params.get("msg", ""),
                "threads": threads,
                "selected": selected,
                "messages": messages,
                "provider_ready": ready,
                "provider_label": provider_label,
                "active_provider": infer_provider(config.llm),
                "active_model": config.llm.model,
                "suggestions": SUGGESTIONS,
                "default_allow_writes": bool(config.agent.allow_writes),
            },
        )

    @router.post("/agent/threads")
    def create_thread_form(conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.require_profile(config, conn)
        thread_id = db.create_agent_thread(
            conn,
            profile.id or 0,
            provider=infer_provider(config.llm),
            model=config.llm.model,
        )
        return RedirectResponse(f"/agent?thread={thread_id}", status_code=303)

    @router.post("/agent/threads/{thread_id}/send")
    def send_form(
        thread_id: int,
        message: str = Form(...),
        allow_writes: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        profile, prefs = services.require_profile(config, conn)
        _thread_for(conn, profile.id or 0, thread_id)
        try:
            AgentRuntime(
                config,
                conn,
                profile,
                prefs,
                allow_writes=allow_writes.strip().lower() in {"1", "on", "true", "yes"},
            ).run(message, thread_id=thread_id)
            return RedirectResponse(
                f"/agent?thread={thread_id}&msg={_msg('Reply added.')}",
                status_code=303,
            )
        except AgentError as exc:
            return RedirectResponse(
                f"/agent?thread={thread_id}&msg={_msg(_safe_error(exc))}",
                status_code=303,
            )

    @router.post("/api/agent/threads")
    def create_thread(conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.require_profile(config, conn)
        thread_id = db.create_agent_thread(
            conn,
            profile.id or 0,
            provider=infer_provider(config.llm),
            model=config.llm.model,
        )
        thread = db.get_agent_thread(conn, thread_id)
        return {"thread": thread}

    @router.get("/api/agent/threads/{thread_id}")
    def get_thread(thread_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.require_profile(config, conn)
        thread = _thread_for(conn, profile.id or 0, thread_id)
        return {"thread": thread, "messages": _messages(conn, thread_id)}

    @router.post("/api/agent/threads/{thread_id}/messages")
    def send_message(
        thread_id: int,
        payload: AgentMessageIn,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        profile, prefs = services.require_profile(config, conn)
        _thread_for(conn, profile.id or 0, thread_id)
        try:
            result = AgentRuntime(
                config,
                conn,
                profile,
                prefs,
                allow_writes=bool(payload.allow_writes),
            ).run(payload.message, thread_id=thread_id)
        except (AgentError, ProviderAuthError, ProviderError) as exc:
            return JSONResponse(
                {"error": _safe_error(exc), "thread_id": thread_id},
                status_code=502,
            )
        return {
            "thread_id": result.thread_id,
            "content": result.content,
            "provider": result.provider,
            "model": result.model,
            "steps": result.steps,
            "usage": result.usage,
            "tool_trace": result.tool_trace,
            "messages": result.new_messages,
            "stopped_reason": result.stopped_reason,
        }

    @router.delete("/api/agent/threads/{thread_id}")
    def delete_thread(thread_id: int, conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.require_profile(config, conn)
        _thread_for(conn, profile.id or 0, thread_id)
        db.delete_agent_thread(conn, thread_id)
        return {"deleted": thread_id}

    return router
