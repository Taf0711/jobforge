"""Deterministic email-to-application matching and status tracking.

The tracker is intentionally not an LLM classifier. It links a message to at
most one application only when company, role, sender, URL, and ATS signals
agree. Ambiguous messages stay unlinked for a human to resolve. A linked
message may advance an application status, but it never downgrades a terminal
state and never submits anything.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import urlsplit

from .. import db
from ..models import (
    ApplicationStatus,
    EmailCategory,
    EmailMessage,
    utcnow,
)

AUTO_LINK_MIN = 45.0
AUTO_STATUS_MIN = 60.0

CATEGORY_STATUS = {
    EmailCategory.CONFIRMATION.value: ApplicationStatus.SUBMITTED.value,
    EmailCategory.ASSESSMENT.value: ApplicationStatus.SCREENING.value,
    EmailCategory.INTERVIEW.value: ApplicationStatus.INTERVIEW.value,
    EmailCategory.OFFER.value: ApplicationStatus.OFFER.value,
    EmailCategory.REJECTION.value: ApplicationStatus.REJECTED.value,
}

STATUS_ORDER = {
    ApplicationStatus.DRAFT.value: 0,
    ApplicationStatus.PREPARED.value: 1,
    ApplicationStatus.SUBMITTED.value: 2,
    ApplicationStatus.SCREENING.value: 3,
    ApplicationStatus.INTERVIEW.value: 4,
    ApplicationStatus.OFFER.value: 5,
}
TERMINAL = {
    ApplicationStatus.OFFER.value,
    ApplicationStatus.REJECTED.value,
    ApplicationStatus.FAILED.value,
    ApplicationStatus.WITHDRAWN.value,
}
SUBMISSION_PROOF = {
    ApplicationStatus.SUBMITTED.value,
    ApplicationStatus.SCREENING.value,
    ApplicationStatus.INTERVIEW.value,
    ApplicationStatus.OFFER.value,
    ApplicationStatus.REJECTED.value,
}

COMPANY_SUFFIXES = {
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "company",
    "plc",
    "gmbh",
    "ag",
    "sa",
    "sas",
    "bv",
    "group",
    "holdings",
    "the",
    "technologies",
    "technology",
    "labs",
    "systems",
    "solutions",
}

TITLE_STOPWORDS = {
    "a",
    "an",
    "and",
    "associate",
    "entry",
    "for",
    "grad",
    "graduate",
    "i",
    "ii",
    "iii",
    "intern",
    "internship",
    "jr",
    "junior",
    "level",
    "new",
    "of",
    "on",
    "remote",
    "senior",
    "sr",
    "staff",
    "the",
    "to",
}

ATS_DOMAINS = {
    "greenhouse.io": "greenhouse",
    "greenhouse-mail.io": "greenhouse",
    "lever.co": "lever",
    "ashbyhq.com": "ashby",
    "workable.com": "workable",
    "smartrecruiters.com": "smartrecruiters",
    "workday.com": "workday",
    "myworkdayjobs.com": "workday",
    "icims.com": "icims",
    "taleo.net": "taleo",
    "successfactors.com": "successfactors",
    "bamboohr.com": "bamboohr",
    "jobvite.com": "jobvite",
    "recruitee.com": "recruitee",
    "rippling.com": "rippling",
    "paylocity.com": "paylocity",
    "paycom.com": "paycom",
    "ukg.com": "ukg",
    "ultipro.com": "ultipro",
}

GENERIC_MAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "yahoo.com",
    "icloud.com",
    "protonmail.com",
    "aol.com",
}


@dataclass(frozen=True)
class EmailMatch:
    application_id: int | None = None
    score: float = 0.0
    reason: str = ""
    ambiguous: bool = False


def _norm(value: str) -> str:
    value = (value or "").lower().replace("&", " and ")
    value = re.sub(r"[^a-z0-9+#]+", " ", value)
    return " ".join(value.split())


def _phrase(text: str, phrase: str) -> bool:
    phrase = _norm(phrase)
    if not phrase:
        return False
    return f" {phrase} " in f" {_norm(text)} "


def _tokens(value: str) -> list[str]:
    return [token for token in _norm(value).split() if token]


def _company_core(company: str) -> str:
    tokens = _tokens(company)
    while tokens and tokens[-1] in COMPANY_SUFFIXES:
        tokens.pop()
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    return " ".join(tokens)


def _title_core(title: str) -> str:
    tokens = [token for token in _tokens(title) if token not in TITLE_STOPWORDS]
    return " ".join(tokens) or _norm(title)


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    a = set(left)
    b = set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _domain(value: str) -> str:
    address = value or ""
    match = re.search(r"[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})", address)
    if match:
        return match.group(1).lower().strip(".")
    return ""


def _ats_family(domain: str) -> str:
    for suffix, family in ATS_DOMAINS.items():
        if domain == suffix or domain.endswith("." + suffix):
            return family
    return ""


def _url_parts(value: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(value or "")
    except ValueError:
        return "", ""
    return (parsed.hostname or "").lower(), (parsed.path or "").lower()


def _company_domain(row: dict[str, Any]) -> str:
    for key in ("url", "apply_url"):
        host, _ = _url_parts(str(row.get(key) or ""))
        if not host or host in GENERIC_MAIL_DOMAINS:
            continue
        if _ats_family(host):
            continue
        labels = [label for label in host.split(".") if label]
        if len(labels) >= 2:
            root = labels[-2]
            if root not in {"www", "jobs", "careers", "apply", "boards"}:
                return root
    return ""


def _application_rows(conn) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.id, a.status, a.ats, a.updated_at, a.submitted_at,
               j.company, j.title, j.url, j.apply_url, j.source
        FROM applications a JOIN jobs j ON j.id=a.job_id
        ORDER BY a.updated_at DESC, a.id DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def _score_application(row: dict[str, Any], sender: str, subject: str, body: str) -> tuple[float, str, bool]:
    header = f"{sender}\n{subject}"
    body_text = (body or "")[:8000]
    full = f"{sender}\n{subject}\n{body_text}"
    company = str(row.get("company") or "")
    core = _company_core(company)
    title = str(row.get("title") or "")
    title_core = _title_core(title)
    # The caller adds the actual sender under a private key.
    sender_domain = _domain(str(row.get("_sender") or ""))
    ats = str(row.get("ats") or "").lower()
    score = 0.0
    reasons: list[str] = []
    header_title = False

    # Try the full company first, then the suffix-stripped core. Short but
    # distinctive names such as "MCP" must still match; "AI" or "X" alone stay
    # out unless another signal (domain, URL, ATS) carries the match.
    company_phrases = [name for name in (_norm(company), core) if name]
    company_phrases = list(dict.fromkeys(company_phrases))
    company_matched = False
    for phrase in company_phrases:
        if len(phrase) < 3 and " " not in phrase:
            continue
        if _phrase(header, phrase):
            score += 55
            reasons.append("company in sender or subject")
            company_matched = True
            break
        if _phrase(body_text, phrase):
            score += 40
            reasons.append("company in message")
            company_matched = True
            break
        if _phrase(full, phrase):
            score += 25
            reasons.append("company in footer")
            company_matched = True
            break
    if not company_matched:
        company_tokens = _tokens(core)
        if len(company_tokens) > 1 and all(
            _phrase(header, token) for token in company_tokens
        ):
            score += 30
            reasons.append("company words in sender or subject")
        elif len(company_tokens) > 1 and all(
            _phrase(body_text, token) for token in company_tokens
        ):
            score += 22
            reasons.append("company words in message")

    company_domain = _company_domain(row)
    if company_domain and len(company_domain) >= 4:
        if company_domain in sender_domain:
            score += 22
            reasons.append("company domain in sender")
        elif company_domain in _norm(body_text):
            score += 8
            reasons.append("company domain in message")

    if title_core and _phrase(header, title):
        score += 30
        reasons.append("role in sender or subject")
        header_title = True
    elif title_core and _phrase(header, title_core):
        score += 24
        reasons.append("role words in subject")
        header_title = True
    elif title_core and _phrase(body_text, title_core):
        score += 14
        reasons.append("role in message")

    if title_core:
        overlap = _jaccard(_tokens(title_core), _tokens(header))
        if overlap >= 0.66:
            score += 12
            reasons.append("role overlap in subject")
            header_title = header_title or overlap >= 0.8
        elif overlap >= 0.45:
            score += 6
            reasons.append("partial role overlap")

    if ats and sender_domain and _ats_family(sender_domain) == ats:
        score += 8
        reasons.append(f"{ats} sender")

    for key in ("url", "apply_url"):
        host, path = _url_parts(str(row.get(key) or ""))
        if host and len(host) >= 6 and host in body_text:
            score += 20
            reasons.append("job URL in message")
            break
        if path and len(path) >= 14 and path in body_text:
            score += 12
            reasons.append("job path in message")
            break

    updated = _parse_dt(str(row.get("updated_at") or ""))
    if updated is not None:
        age = datetime.now(timezone.utc) - updated
        if age <= timedelta(days=180):
            score += 3

    return score, ", ".join(reasons), header_title


def _parse_dt(value: str) -> datetime | None:
    if not value:
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


def match_email(conn, sender: str, subject: str, body: str) -> EmailMatch:
    """Return the best single application for one message, or an explicit miss."""
    rows = _application_rows(conn)
    if not rows:
        return EmailMatch(reason="no applications")
    scored: list[tuple[float, str, bool, dict[str, Any]]] = []
    for row in rows:
        row = dict(row)
        row["_sender"] = sender
        score, reason, header_title = _score_application(row, sender, subject, body)
        if score >= 28:
            scored.append((score, reason, header_title, row))
    if not scored:
        return EmailMatch(reason="no company or sender signal")
    scored.sort(key=lambda item: (-item[0], str(item[3].get("company") or ""), int(item[3]["id"])))
    top_score, top_reason, top_header_title, top = scored[0]
    if not any(
        marker in top_reason
        for marker in ("company", "job URL", "job path")
    ):
        return EmailMatch(score=top_score, reason=f"no company signal: {top_reason}")
    if top_score < AUTO_LINK_MIN:
        return EmailMatch(score=top_score, reason=f"below match threshold: {top_reason}")

    if len(scored) > 1:
        second_score, second_reason, second_header_title, second = scored[1]
        close = top_score - second_score < 12 and top_score < 80
        same_company = _norm(str(top.get("company") or "")) == _norm(
            str(second.get("company") or "")
        )
        if close and not (top_header_title and not second_header_title):
            names = f"{top.get('company')} / {second.get('company')}"
            return EmailMatch(
                score=top_score,
                reason=f"ambiguous: {names}; {top_reason} vs {second_reason}",
                ambiguous=True,
            )
        if same_company and top_score - second_score < 20 and not top_header_title:
            return EmailMatch(
                score=top_score,
                reason=(
                    f"ambiguous same company: {top.get('title')} vs {second.get('title')}; "
                    f"{top_reason}"
                ),
                ambiguous=True,
            )

    return EmailMatch(
        application_id=int(top["id"]),
        score=round(top_score, 1),
        reason=top_reason,
    )


def allowed_transition(current: str, target: str) -> bool:
    current = (current or "").lower()
    target = (target or "").lower()
    if not target or current == target:
        return False
    if current in TERMINAL:
        return False
    if target in {ApplicationStatus.FAILED.value, ApplicationStatus.WITHDRAWN.value}:
        return False
    if target == ApplicationStatus.REJECTED.value:
        return current in {
            ApplicationStatus.DRAFT.value,
            ApplicationStatus.PREPARED.value,
            ApplicationStatus.SUBMITTED.value,
            ApplicationStatus.SCREENING.value,
            ApplicationStatus.INTERVIEW.value,
        }
    return STATUS_ORDER.get(target, -1) > STATUS_ORDER.get(current, -1)


def _ensure_message_id(msg: EmailMessage) -> None:
    if msg.message_id:
        return
    basis = f"{msg.sender}|{msg.subject}|{msg.received_at}".encode("utf-8")
    msg.message_id = "local:" + hashlib.sha256(basis).hexdigest()[:32]


def _event_detail(
    category: str,
    before: str,
    after: str,
    msg: EmailMessage,
    score: float,
) -> str:
    subject = re.sub(r"\s+", " ", msg.subject or "")[:140]
    sender = re.sub(r"\s+", " ", msg.sender or "")[:100]
    return (
        f"{category}: {before or 'unknown'} -> {after}; subject={subject!r}; "
        f"from={sender!r}; match={score:.1f}"
    )


def _process_saved_email(
    conn,
    config: Any,
    email_id: int,
    *,
    allow_status: bool = True,
) -> dict[str, Any]:
    row = db.get_email(conn, email_id)
    if not email_id or not row:
        return {"changed": False, "reason": "missing email"}
    application_id = row.get("application_id")
    if not application_id:
        db.mark_email_processed(conn, email_id, status_before="", status_after="")
        return {"matched": False, "changed": False, "reason": "unmatched"}

    application = db.get_application(conn, int(application_id))
    if application is None:
        db.mark_email_processed(conn, email_id, status_before="", status_after="")
        return {"matched": False, "reason": "application missing"}

    # Re-fetching the same IMAP message must not replay a status event.
    if row.get("processed_at") and str(row.get("status_after") or ""):
        return {
            "matched": True,
            "changed": False,
            "application_id": int(application_id),
            "reason": "already processed",
        }

    current = str(application.status or "")
    category = str(row.get("category") or "")
    target = CATEGORY_STATUS.get(category, "")
    source = str(conn.execute("SELECT match_source FROM emails WHERE id=?", (email_id,)).fetchone()["match_source"] or "")
    score = float(row.get("match_score") or 0)
    changed = False
    reason = ""
    if not allow_status:
        # Leave the message unprocessed so a later apply_status=true call can
        # still advance the stage. Outside-window messages are retried cheaply.
        return {
            "matched": True,
            "changed": False,
            "application_id": int(application_id),
            "reason": "status changes are disabled or outside the status window",
        }
    if not target:
        reason = "no status change for this category"
    elif source != "manual" and score < AUTO_STATUS_MIN:
        reason = f"low match confidence ({score:.1f})"
    elif not allowed_transition(current, target):
        reason = f"would not advance {current} to {target}"
    else:
        fields: dict[str, Any] = {"status": target, "status_source": "email"}
        if not application.submitted_at and target in SUBMISSION_PROOF:
            submitted = str(row.get("received_at") or "") or utcnow()
            fields["submitted_at"] = submitted
        db.update_application(conn, int(application_id), **fields)
        msg = db.email_from_row(row)
        db.log_event(
            conn,
            int(application_id),
            "email_status",
            _event_detail(category, current, target, msg, score),
        )
        changed = True
        reason = f"{current} -> {target}"

    db.mark_email_processed(
        conn,
        email_id,
        status_before=current,
        status_after=target if changed else current,
    )
    return {
        "matched": True,
        "changed": changed,
        "application_id": int(application_id),
        "before": current,
        "after": target if changed else current,
        "reason": reason,
    }


def process_email(conn, config: Any, email_id: int, *, allow_status: bool = True) -> dict[str, Any]:
    """Expose one-message processing for manual links and tests."""
    return _process_saved_email(conn, config, email_id, allow_status=allow_status)


def process_messages(
    conn, config: Any, messages: Iterable[EmailMessage], *, apply_status: bool = True
) -> dict[str, Any]:
    """Save, match, and status-track a batch of messages.

    ``apply_status=False`` still dedupes and links messages, but leaves every
    application stage untouched. Host-agent ingestion uses this for a dry run.
    """
    summary: dict[str, Any] = {
        "enabled": True,
        "fetched": 0,
        "saved": 0,
        "new": 0,
        "matched": 0,
        "unmatched": 0,
        "ambiguous": 0,
        "status_changes": 0,
        "categories": {},
        "changes": [],
        "errors": [],
    }
    for msg in messages:
        summary["fetched"] += 1
        try:
            _ensure_message_id(msg)
            existing = conn.execute(
                "SELECT id, match_source FROM emails WHERE message_id=?",
                (msg.message_id,),
            ).fetchone()
            is_new = existing is None
            manual = bool(existing and str(existing["match_source"] or "") in {"manual", "manual_ignore"})
            if not manual:
                match = match_email(conn, msg.sender, msg.subject, msg.body)
                msg.application_id = match.application_id
                msg.match_score = match.score
                msg.match_reason = match.reason
                msg.match_source = "auto" if match.application_id else ""
                if match.ambiguous:
                    summary["ambiguous"] += 1
            email_id = db.save_email(conn, msg, replace_match=not manual)
            summary["saved"] += 1
            if is_new:
                summary["new"] += 1
            category = str(msg.category or EmailCategory.OTHER.value)
            summary["categories"][category] = summary["categories"].get(category, 0) + 1
            received = _parse_dt(str(msg.received_at or ""))
            cutoff = datetime.now(timezone.utc) - timedelta(
                days=max(1, int(getattr(config, "status_window_days", 60) or 60))
            )
            allow_status = bool(apply_status) and (received is None or received >= cutoff)
            result = _process_saved_email(conn, config, email_id, allow_status=allow_status)
            if result.get("matched"):
                summary["matched"] += 1
            else:
                summary["unmatched"] += 1
            if result.get("changed"):
                summary["status_changes"] += 1
                row = db.get_email(conn, email_id) or {}
                summary["changes"].append(
                    {
                        "email_id": email_id,
                        "application_id": result.get("application_id"),
                        "company": row.get("company") or "",
                        "title": row.get("title") or "",
                        "before": result.get("before") or "",
                        "after": result.get("after") or "",
                        "category": category,
                        "subject": re.sub(r"\s+", " ", msg.subject or "")[:140],
                    }
                )
        except Exception as exc:  # noqa: BLE001 - one bad message must not abort the inbox
            summary["errors"].append({"message_id": msg.message_id, "error": str(exc)})

    # Retry older unmatched messages lazily; an application may have been
    # created after the first poll.
    summary["rematched"] = reprocess_unmatched(conn, config, apply_status=apply_status)
    return summary


def reprocess_unmatched(
    conn, config: Any, limit: int = 150, *, apply_status: bool = True
) -> int:
    """Try to link previously unmatched messages without re-fetching IMAP."""
    rows = conn.execute(
        """
        SELECT * FROM emails
        WHERE application_id IS NULL
          AND COALESCE(match_source,'') <> 'manual_ignore'
        ORDER BY received_at DESC, id DESC
        LIMIT ?
        """,
        (max(1, limit),),
    ).fetchall()
    linked = 0
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max(1, int(getattr(config, "status_window_days", 60) or 60))
    )
    for row in rows:
        email_row = dict(row)
        match = match_email(
            conn,
            str(email_row.get("sender") or ""),
            str(email_row.get("subject") or ""),
            str(email_row.get("body") or ""),
        )
        if not match.application_id:
            continue
        db.link_email(
            conn,
            int(email_row["id"]),
            match.application_id,
            source="auto",
            score=match.score,
            reason=match.reason,
        )
        received = _parse_dt(str(email_row.get("received_at") or ""))
        allow_status = (
            bool(apply_status) and received is not None and received >= cutoff
        )
        _process_saved_email(conn, config, int(email_row["id"]), allow_status=allow_status)
        linked += 1
    return linked


def tracker_overview(conn) -> dict[str, Any]:
    """Small read-only summary for the applications page."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    rows = conn.execute(
        """
        SELECT
          SUM(CASE WHEN application_id IS NULL AND category <> 'other'
                        AND COALESCE(match_source,'') <> 'manual_ignore'
                   THEN 1 ELSE 0 END) AS unmatched,
          SUM(CASE WHEN application_id IS NULL AND category = 'other'
                        AND COALESCE(match_source,'') <> 'manual_ignore'
                   THEN 1 ELSE 0 END) AS unmatched_other,
          SUM(CASE WHEN status_before <> '' AND status_after <> ''
                        AND status_before <> status_after THEN 1 ELSE 0 END) AS changes,
          SUM(CASE WHEN status_before <> '' AND status_after <> ''
                        AND status_before <> status_after
                        AND processed_at >= ? THEN 1 ELSE 0 END) AS recent_changes
        FROM emails
        """,
        (cutoff,),
    ).fetchone()
    return {
        "unmatched": int(rows["unmatched"] or 0) if rows else 0,
        "unmatched_other": int(rows["unmatched_other"] or 0) if rows else 0,
        "changes": int(rows["changes"] or 0) if rows else 0,
        "recent_changes": int(rows["recent_changes"] or 0) if rows else 0,
    }


def last_poll_summary(conn) -> dict[str, Any]:
    raw = db.setting_get(conn, "email.last_poll", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    data["at"] = db.setting_get(conn, "email.last_poll_at", "")
    data["error"] = db.setting_get(conn, "email.last_error", "")
    return data
