"""IMAP poller for application verification codes and employer replies."""

from __future__ import annotations

import email
import html
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from email.message import Message
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Callable

from .. import db
from ..config import EmailConfig
from ..models import EmailCategory, EmailMessage
from .credentials import load_password

CODE_CONTEXT = re.compile(
    r"(?:code|pin|verification|verify|security)\D{0,24}(\d{4,8})|(\d{4,8})\D{0,24}(?:code|pin|verification)",
    re.IGNORECASE,
)
DIGITS = re.compile(r"\b(\d{4,8})\b")

# A reply is classified from fresh text only. Quoted threads frequently contain
# an older rejection or offer and must not re-trigger a status change.
QUOTE_MARKERS = re.compile(
    r"(?im)^(?:on .+ wrote:|-{2,}\s*original message\s*-{2,}|from:\s.+|sent:\s.+|"
    r"_{10,}|>{1,}\s*)"
)

REJECTION = re.compile(
    r"\b(?:unfortunately|regret to inform|not (?:moving|going) forward|"
    r"unable to (?:offer|move forward|proceed)|cannot offer|can't offer|"
    r"not selected|other candidates|not advancing|no longer under consideration|"
    r"position (?:has been|was) filled|decided not to (?:proceed|move forward)|"
    r"not a fit|application (?:was )?unsuccessful|not successful|"
    r"we(?:'ve| have) decided to move forward with other)\b",
    re.IGNORECASE,
)
OFFER = re.compile(
    r"\b(?:pleased to offer|happy to offer|delighted to offer|thrilled to offer|"
    r"offer of employment|formal offer|job offer|offer letter|"
    r"extend(?:ing)? (?:you )?an offer|offer for the (?:position|role|job))\b",
    re.IGNORECASE,
)
ASSESSMENT = re.compile(
    r"\b(?:coding challenge|technical assessment|online assessment|take[- ]home|"
    r"hackerrank|codesignal|codility|assessment (?:link|invitation|exercise)|"
    r"complete (?:the|your) assessment|skills assessment)\b",
    re.IGNORECASE,
)
INTERVIEW = re.compile(
    r"\b(?:interview invitation|interview request|interview availability|"
    r"invite you (?:to|for) an interview|schedule (?:an? )?(?:interview|call|conversation|chat)|"
    r"arrange (?:an? )?(?:interview|call|conversation)|"
    r"would like to (?:meet|speak|chat|talk)|phone screen|technical screen|"
    r"recruiter screen|screening call|next round|onsite interview|"
    r"hiring manager (?:call|chat|interview))\b",
    re.IGNORECASE,
)
CONFIRMATION = re.compile(
    r"\b(?:application received|received your application|thank you for applying|"
    r"thanks for applying|application submitted|we(?:'ve| have) received your application|"
    r"application is under review|your application (?:is|has been) received)\b",
    re.IGNORECASE,
)
VERIFICATION = re.compile(
    r"\b(?:verification code|security code|verify your email|confirm your email|"
    r"one[- ]time (?:code|passcode|password)|authentication code)\b",
    re.IGNORECASE,
)


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


def _html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", value)
    value = re.sub(r"<[^>]+>", " ", value)
    return html.unescape(value)


def _body_text(msg: Message) -> str:
    if msg.is_multipart():
        plain: list[str] = []
        rendered: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True) or b""
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if ctype == "text/html":
                rendered.append(_html_to_text(text))
            else:
                plain.append(text)
        return "\n".join(plain) if plain else "\n".join(rendered)
    payload = msg.get_payload(decode=True) or b""
    text = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return _html_to_text(text) if msg.get_content_type() == "text/html" else text


def _fresh_body(body: str, limit: int = 12_000) -> str:
    text = (body or "").replace("\r\n", "\n").replace("\r", "\n")
    match = QUOTE_MARKERS.search(text)
    if match:
        text = text[: match.start()]
    lines = [line for line in text.split("\n") if not line.lstrip().startswith(">")]
    return "\n".join(lines)[:limit]


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
    """Return a conservative email category.

    Rejection is checked before offer so "unable to offer" is not an offer.
    Verification is checked first because a code can arrive with marketing
    copy that mentions applications.
    """
    fresh = _fresh_body(body)
    text = f"{subject}\n{fresh}"
    if VERIFICATION.search(text):
        return EmailCategory.VERIFICATION.value
    if REJECTION.search(text):
        return EmailCategory.REJECTION.value
    if OFFER.search(text):
        return EmailCategory.OFFER.value
    if ASSESSMENT.search(text):
        return EmailCategory.ASSESSMENT.value
    if INTERVIEW.search(text):
        return EmailCategory.INTERVIEW.value
    if CONFIRMATION.search(text):
        return EmailCategory.CONFIRMATION.value
    return EmailCategory.OTHER.value


def _received_at(msg: Message) -> str:
    raw = msg.get("Date")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError, OverflowError):
            pass
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sender_address(value: str) -> str:
    return parseaddr(value or "")[1].strip().lower()


def _match_application(conn, sender: str, subject: str, body: str) -> int | None:
    """Compatibility shim for old callers.

    New code uses :func:`jobforge.email.tracker.match_email` and receives the
    score/reason as well. This function keeps the previous weak behavior out of
    the live poll path.
    """
    from .tracker import match_email

    match = match_email(conn, sender, subject, body)
    return match.application_id


def _connect(config: EmailConfig) -> imaplib.IMAP4_SSL:
    if not config.username:
        raise RuntimeError("Set email.username in Settings or JOBFORGE_IMAP_USER first")
    password = load_password(config)
    if not password:
        raise RuntimeError(
            "Set the IMAP password in Settings (stored in the OS keychain or a 0600 file) "
            f"or in {config.password_env}"
        )
    client = imaplib.IMAP4_SSL(config.host, config.port, timeout=max(5, config.timeout_seconds))
    try:
        client.login(config.username, password)
    except Exception:
        try:
            client.logout()
        except Exception:  # noqa: BLE001
            pass
        raise
    return client


def test_connection(config: EmailConfig) -> dict[str, Any]:
    with _connect(config) as client:
        status, _ = client.select(config.folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open folder {config.folder!r}")
    return {"ok": True, "host": config.host, "folder": config.folder}


def fetch_recent(config: EmailConfig, limit: int | None = None) -> list[EmailMessage]:
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, config.lookback_days))).strftime(
        "%d-%b-%Y"
    )
    limit = max(1, min(1000, int(limit or config.max_messages)))
    messages: list[EmailMessage] = []
    with _connect(config) as client:
        status, _ = client.select(config.folder, readonly=True)
        if status != "OK":
            raise RuntimeError(f"Could not open folder {config.folder!r}")
        status, data = client.search(None, f'(SINCE "{since}")')
        if status != "OK":
            return []
        ids = (data[0] or b"").split()[-limit:]
        for msg_id in ids:
            # PEEK leaves the user's unread state untouched.
            status, payload = client.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not payload:
                continue
            raw = payload[0][1] if isinstance(payload[0], tuple) else b""
            if not raw:
                continue
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


def poll(
    config: EmailConfig,
    conn,
    *,
    fetcher: Callable[[EmailConfig, int], list[EmailMessage]] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Fetch and process recent mail. The tracker never submits applications."""
    if not config.enabled:
        raise RuntimeError("Email sync is disabled in Settings")
    if not config.username:
        raise RuntimeError("Set email.username in Settings or JOBFORGE_IMAP_USER first")
    if fetcher is None:
        messages = fetch_recent(config, limit)
    else:
        messages = fetcher(config, limit or config.max_messages)
    from .tracker import process_messages

    return process_messages(conn, config, messages)
