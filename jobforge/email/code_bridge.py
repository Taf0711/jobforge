"""Deterministic wait-for-verification-code bridge.

The browser flow calls this only after the user opts in and the open page has a
verification-code field. This module never submits a form and never logs a full
code: codes are secrets and must only reach the page field.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .. import db
from ..models import EmailCategory, EmailMessage


@dataclass
class CodeRequest:
    company: str = ""
    sender_hints: list[str] = field(default_factory=list)
    subject_hints: list[str] = field(default_factory=list)
    since_iso: str = ""
    timeout_s: int = 120
    poll_interval_s: float = 3.0
    application_id: int | None = None


@dataclass
class CodeResult:
    matched: bool
    code: str = ""
    message_id: str = ""
    sender: str = ""
    subject: str = ""
    received_at: str = ""
    elapsed_s: float = 0.0
    reason: str = ""


def _parse_utc(value: str) -> datetime | None:
    if not value or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hints(*values: str, extra: Iterable[str] = ()) -> list[str]:
    needles = [str(v).strip().lower() for v in values if str(v).strip()]
    needles.extend(str(v).strip().lower() for v in extra if str(v).strip())
    return needles


def match_message(msg: EmailMessage, request: CodeRequest) -> bool:
    """Return True only for a fresh verification message tied to this request.

    A code alone is never sufficient: the message must also match the company
    or one of the configured sender/subject hints.
    """
    category = getattr(msg.category, "value", msg.category)
    if str(category).strip().lower() != EmailCategory.VERIFICATION.value:
        return False
    if not (msg.verification_code or "").strip():
        return False

    since = _parse_utc(request.since_iso)
    received = _parse_utc(msg.received_at)
    if since is None or received is None or received < since:
        return False

    needles = _hints(request.company, extra=[*request.sender_hints, *request.subject_hints])
    if not needles:
        return False
    haystack = f"{msg.sender}\n{msg.subject}\n{msg.body}".lower()
    return any(needle in haystack for needle in needles)


def redact_code(code: str) -> str:
    """Return a display-safe form that never reveals the whole code."""
    code = (code or "").strip()
    if not code:
        return ""
    if len(code) <= 2:
        return "\u2022" * len(code)
    return "\u2022" * (len(code) - 2) + code[-2:]


def wait_for_code(
    conn: Any,
    config: Any,
    request: CodeRequest,
    *,
    fetcher: Callable[[Any], Iterable[EmailMessage]] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> CodeResult:
    """Poll the inbox until a matching code arrives or the request times out.

    Injected ``fetcher``/``sleeper``/``clock`` make the wait deterministic and
    offline in tests. ``fetcher`` receives ``config`` and must return
    ``EmailMessage`` objects, matching the default ``fetch_recent(config.email)``
    behavior. Fetcher errors (for example IMAP auth failures) propagate.
    """
    if fetcher is None:

        def default_fetcher(cfg: Any) -> list[EmailMessage]:
            from .imap import fetch_recent

            return fetch_recent(cfg.email)

        fetcher = default_fetcher

    start = clock()
    seen_ids: set[str] = set()
    timeout_s = float(request.timeout_s)

    while True:
        for msg in fetcher(config):
            message_id = (msg.message_id or "").strip()
            if message_id:
                if message_id in seen_ids:
                    continue
                seen_ids.add(message_id)
            if not match_message(msg, request):
                continue
            return CodeResult(
                matched=True,
                code=msg.verification_code.strip(),
                message_id=message_id,
                sender=msg.sender or "",
                subject=msg.subject or "",
                received_at=msg.received_at or "",
                elapsed_s=clock() - start,
                reason="matched",
            )

        elapsed = clock() - start
        if elapsed >= timeout_s:
            return CodeResult(matched=False, elapsed_s=elapsed, reason="timeout")

        remaining = timeout_s - elapsed
        delay = min(float(request.poll_interval_s), remaining)
        if delay > 0:
            sleeper(delay)

        if clock() - start >= timeout_s:
            return CodeResult(matched=False, elapsed_s=clock() - start, reason="timeout")


def redact_code_in_text(value: str, code: str) -> str:
    """Redact a code even when the text formats it with separators.

    ``123456``, ``123 456``, ``123-456``, ``123.456``, and ``123_456`` all
    match. A different digit group is left untouched.
    """
    if not value or not code:
        return value or ""

    pattern = re.compile(r"\d(?:[\s._-]?\d){3,}")

    def replace(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        return redact_code(code) if digits == code else match.group(0)

    return pattern.sub(replace, value)


def _redact_field(value: str, code: str) -> str:
    """Keep metadata safe even if the code appears inside a subject or id."""
    return redact_code_in_text(value, code)


def record_bridge_event(conn: Any, application_id: int | None, result: CodeResult) -> None:
    """Append a ``verification_code`` event whose detail is code-redacted."""
    elapsed = f"{result.elapsed_s:.1f}s"
    if result.matched:
        detail = (
            f"matched message_id={_redact_field(result.message_id, result.code)} "
            f"sender={_redact_field(result.sender, result.code)} "
            f"subject={_redact_field(result.subject, result.code)} "
            f"received_at={result.received_at} "
            f"code={redact_code(result.code)} elapsed={elapsed}"
        )
    else:
        detail = f"no match reason={result.reason} elapsed={elapsed}"
    db.log_event(conn, application_id, "verification_code", detail)
