"""Ingest employer mail that a host agent already read through its connector.

JobForge does not fetch arbitrary mailboxes here. The host agent supplies
normalized message fields, and this layer applies the same deterministic
classifier, matcher, dedupe, and forward-only status rules as the IMAP poller.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

from ..models import EmailMessage, utcnow
from .imap import classify
from .tracker import process_messages

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PROVIDER_RE = re.compile(r"[^a-z0-9._-]+")


def _clean(value: Any, limit: int) -> str:
    text = _CONTROL_RE.sub("", str(value or "")).replace("\r\n", "\n").replace("\r", "\n")
    return text.strip()[:limit]


def _received_at(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return utcnow()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return utcnow()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _provider(value: Any) -> str:
    normalized = _PROVIDER_RE.sub("-", str(value or "agent").strip().lower()).strip("-")
    return (normalized or "agent")[:40]


def ingest_messages(
    conn,
    config: Any,
    provider: str,
    rows: Iterable[dict[str, Any]],
    *,
    apply_status: bool = True,
) -> dict[str, Any]:
    """Validate host-supplied messages, then save/match/track them.

    The message id is prefixed with ``host:<provider>:`` so a Gmail message and
    an Outlook message with the same opaque id cannot collide.
    """
    provider_value = _provider(provider)
    messages: list[EmailMessage] = []
    rejected: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        try:
            message_id = _clean(row.get("message_id"), 512)
            if not message_id:
                raise ValueError("message_id is required")
            sender = _clean(row.get("sender"), 1_000)
            subject = _clean(row.get("subject"), 2_000)
            body = _clean(row.get("body"), 60_000)
            if not (sender or subject or body):
                raise ValueError("sender, subject, or body is required")
            messages.append(
                EmailMessage(
                    message_id=f"host:{provider_value}:{message_id}",
                    sender=sender,
                    subject=subject,
                    body=body,
                    received_at=_received_at(row.get("received_at")),
                    category=classify(subject, body),
                )
            )
        except Exception as exc:  # noqa: BLE001 - report one bad row, keep the batch
            rejected.append({"index": index, "error": str(exc)})

    summary = process_messages(
        conn,
        config,
        messages,
        apply_status=apply_status,
    )
    summary["provider"] = provider_value
    summary["accepted"] = len(messages)
    summary["rejected"] = len(rejected)
    summary["ingest_errors"] = rejected
    return summary
