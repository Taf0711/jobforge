"""Answer-bank editor router.

Mount this on the main FastAPI app with ``create_answers_router(config)``.
It owns ``/answers`` (HTML form) and ``/api/answers`` (JSON read/replace) and
writes ``<data_dir>/answers.json`` atomically with mode 0600.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, Generator
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import db, services
from ..automation.drafter import (
    DraftError,
    approve_draft,
    draft_answer,
    list_drafts,
    reject_draft,
)
from ..config import Config
from ..locking import action_lock

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

MAX_ENTRIES = 200
MAX_KEY_CHARS = 120
MAX_VALUE_CHARS = 500

# Display-only document of the built-in rules in jobforge/automation/answers.py.
BUILTIN_RULES: list[tuple[str, str]] = [
    ("Name", "first name, last name, full name"),
    ("Email", "e-mail"),
    ("Phone", "phone, mobile, telephone"),
    ("Location", "city, current location, where based"),
    ("Current company", "current company, current employer"),
    ("Current title", "current title, current role"),
    ("Work authorization", "legally authorized to work"),
    ("Sponsorship", "require or need visa sponsorship"),
    ("Salary", "salary, compensation, desired pay"),
    ("Notice period", "notice period, start date, availability"),
    ("Relocation", "willing to relocate"),
    ("Clearance", "security clearance"),
    ("Voluntary disclosures", "veteran, disability, gender, race, orientation"),
]


def answers_path(config: Config) -> Path:
    return Path(config.data_dir) / "answers.json"


def read_overrides(path: Path) -> dict[str, str]:
    """Load string-to-string overrides. A missing or malformed file is empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}


def _check_text(label: str, text: str, limit: int) -> None:
    if not 1 <= len(text) <= limit:
        raise ValueError(f"{label} must be 1-{limit} characters")
    if any(ch in "<>" or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise ValueError(f"{label} may not contain '<', '>', or control characters")


def validate_overrides(data: Any) -> dict[str, str]:
    """Validate a full override mapping, returning a trimmed key copy."""
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object mapping question substrings to answers")
    if len(data) > MAX_ENTRIES:
        raise ValueError(f"Too many overrides (max {MAX_ENTRIES})")

    cleaned: dict[str, str] = {}
    for raw_key, raw_value in data.items():
        if not isinstance(raw_key, str) or not isinstance(raw_value, str):
            raise ValueError("Keys and values must be strings")
        key = raw_key.strip()
        _check_text("Key", key, MAX_KEY_CHARS)
        _check_text("Value", raw_value, MAX_VALUE_CHARS)
        cleaned[key] = raw_value
    return cleaned


def write_overrides(path: Path, overrides: dict[str, str]) -> None:
    """Write JSON atomically: temp file in the same directory, fsync, replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(overrides, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".answers-", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
        os.chmod(path, 0o600)
    finally:
        tmp_path.unlink(missing_ok=True)


def create_answers_router(config: Config) -> APIRouter:
    router = APIRouter()

    def get_conn() -> Generator[sqlite3.Connection, None, None]:
        conn = db.connect(config.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @router.get("/answers", response_class=HTMLResponse)
    def answers_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        profile, _ = services.require_profile(config, conn)
        overrides = read_overrides(answers_path(config))
        return templates.TemplateResponse(
            request=request,
            name="answers.html",
            context={
                "request": request,
                "profile": profile,
                "msg": request.query_params.get("msg", ""),
                "overrides": dict(sorted(overrides.items())),
                "drafts": list_drafts(config),
                "rules": BUILTIN_RULES,
                "features": config.features,
                "max_entries": MAX_ENTRIES,
                "max_key_chars": MAX_KEY_CHARS,
                "max_value_chars": MAX_VALUE_CHARS,
            },
        )

    @router.post("/answers")
    def save_answer(
        key: str = Form(""),
        value: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        path = answers_path(config)
        try:
            incoming = validate_overrides({key: value})
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        new_key, new_value = next(iter(incoming.items()))
        with action_lock(config):
            # Re-read under the lock: another writer may have changed the bank
            # while this request was validating its input.
            overrides = read_overrides(path)
            if new_key not in overrides and len(overrides) >= MAX_ENTRIES:
                raise HTTPException(
                    status_code=400, detail=f"Too many overrides (max {MAX_ENTRIES})"
                )
            overrides[new_key] = new_value
            write_overrides(path, overrides)
        return RedirectResponse("/answers", status_code=303)

    @router.post("/answers/draft")
    def create_answer_draft(
        job_id: int = Form(...),
        question: str = Form(""),
        category: str = Form("motivation"),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            draft = draft_answer(config, conn, job_id, question, category=category)
        except DraftError as exc:
            return RedirectResponse(
                f"/answers?msg={quote(str(exc))}", status_code=303
            )
        return RedirectResponse(
            f"/answers?msg={quote('Draft generated. Review it below before approving.')}",
            status_code=303,
        )

    @router.post("/answers/drafts/{draft_id}/approve")
    def approve_answer_draft(
        draft_id: str,
        text: str = Form(""),
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            approve_draft(config, draft_id, text=text)
        except DraftError as exc:
            return RedirectResponse(
                f"/answers?msg={quote(str(exc))}", status_code=303
            )
        return RedirectResponse(
            f"/answers?msg={quote('Draft approved and added to the answer bank.')}",
            status_code=303,
        )

    @router.post("/answers/drafts/{draft_id}/reject")
    def reject_answer_draft(
        draft_id: str,
        conn: sqlite3.Connection = Depends(get_conn),
    ):
        try:
            reject_draft(config, draft_id)
        except DraftError as exc:
            return RedirectResponse(
                f"/answers?msg={quote(str(exc))}", status_code=303
            )
        return RedirectResponse(
            f"/answers?msg={quote('Draft rejected. It was not added to the answer bank.')}",
            status_code=303,
        )

    @router.get("/api/answers/drafts")
    def api_answer_drafts(conn: sqlite3.Connection = Depends(get_conn)):
        return [draft.to_dict() for draft in list_drafts(config)]

    @router.get("/api/answers")
    def api_answers(conn: sqlite3.Connection = Depends(get_conn)):
        return read_overrides(answers_path(config))

    @router.put("/api/answers")
    async def replace_answers(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="Request body must be valid JSON") from None
        try:
            overrides = validate_overrides(data)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        with action_lock(config):
            write_overrides(answers_path(config), overrides)
        return overrides

    return router
