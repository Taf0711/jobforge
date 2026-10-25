"""IMAP poller for application verification codes and employer replies."""

from __future__ import annotations

import email
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Any

from .. import db
from ..config import EmailConfig
from ..models import EmailCategory, EmailMessage

CODE_CONTEXT = re.compile(
    r"(?:code|pin|verification|verify|security)\D{0,24}(\d{4,8})|(\d{4,8})\D{0,24}(?:code|pin|verification)",
    re.IGNORECASE,
)
DIGITS = re.compile(r"\b(\d{4,8})\b")


def _decode(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for text, charset in parts:
        if isinstance(text, bytes):
            out.append(text.decode(charset or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def _body_text(msg: Message) -> str:
    if msg.is_multipart():
        chunks: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in {"text/plain", "text/html"}:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                if ctype == "text/html":
                    text = re.sub(r"<[^>]+>", " ", text)
                chunks.append(text)
        return "\n".join(chunks)
    payload = msg.get_payload(decode=True) or b""
    return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")


def extract_code(subject: str, body: str) -> str:
    haystack = f"{subject}\n{body}"
    for match in CODE_CONTEXT.finditer(haystack):
        code = match.group(1) or match.group(2)
        if code:
            return code
    # Fall back to the first standalone 4–8 digit number in a short email.
    if len(haystack) < 4000:
        match = DIGITS.search(haystack)
        if match:
            return match.group(1)
    return ""


def classify(subject: str, body: str) -> str:
    text = f"{subject} {body}".lower()
    if re.search(r"verification code|security code|verify your email|confirm your email|one-time", text):
        return EmailCategory.VERIFICATION.value
    if re.search(r"offer letter|job offer|pleased to offer|offer of employment", text):
        return EmailCategory.OFFER.value
    if re.search(r"interview|phone screen|schedule a (call|conversation)|next round|onsite", text):
        return EmailCategory.INTERVIEW.value
    if re.search(r"unfortunately|not moving forward|unable to offer|other candidates|not selected|regret to inform", text):
        return EmailCategory.REJECTION.value
    if re.search(r"application received|thank you for applying|we received your application|application submitted", text):
        return EmailCategory.CONFIRMATION.value
    return EmailCategory.OTHER.value


def _received_at(msg: Message) -> str:
    raw = msg.get("Date")
    if raw:
        try:
            return parsedate_to_datetime(raw).astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            pass
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _match_application(conn, sender: str, subject: str, body: str) -> int | None:
    haystack = f"{sender} {subject} {body[:2000]}".lower()
    rows = conn.execute(
        "SELECT a.id, j.company FROM applications a JOIN jobs j ON j.id=a.job_id"
    ).fetchall()
    for row in rows:
        company = str(row["company"] or "").lower().strip()
        if company and company in haystack:
            return int(row["id"])
    return None


def fetch_recent(config: EmailConfig, limit: int = 50) -> list[EmailMessage]:
    if not config.username or not config.password:
        raise RuntimeError("Set email.username and the IMAP password env var first")
    since = (datetime.now(timezone.utc) - timedelta(days=config.lookback_days)).strftime("%d-%b-%Y")
    messages: list[EmailMessage] = []
    with imaplib.IMAP4_SSL(config.host, config.port) as client:
        client.login(config.username, config.password)
        client.select(config.folder)
        status, data = client.search(None, f'(SINCE "{since}")')
        if status != "OK":
            return []
        ids = (data[0] or b"").split()[-limit:]
        for msg_id in ids:
            status, payload = client.fetch(msg_id, "(RFC822)")
            if status != "OK" or not payload:
                continue
            raw = payload[0][1] if isinstance(payload[0], tuple) else b""
            msg = email.message_from_bytes(raw)
            subject = _decode(msg.get("Subject"))
            sender = _decode(msg.get("From"))
            body = _body_text(msg)
            messages.append(
                EmailMessage(
                    message_id=str(msg.get("Message-ID") or "").strip(),
                    sender=sender,
                    subject=subject,
                    body=body[:60_000],
                    received_at=_received_at(msg),
                    category=classify(subject, body),
                    verification_code=extract_code(subject, body),
                )
            )
    return messages


def poll(config: EmailConfig, conn) -> dict[str, Any]:
    messages = fetch_recent(config)
    saved = 0
    categories: dict[str, int] = {}
    for msg in messages:
        msg.application_id = _match_application(conn, msg.sender, msg.subject, msg.body)
        db.save_email(conn, msg)
        saved += 1
        categories[msg.category] = categories.get(msg.category, 0) + 1
    return {"fetched": len(messages), "saved": saved, "categories": categories}
