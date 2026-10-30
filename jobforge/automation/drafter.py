"""Human-approved LLM drafts for free-text application questions.

The drafter never fills a form and never approves its own output. It proposes a
short answer from the job description and verified candidate facts, stores it as
a draft, and only a human approval writes the text into the answer bank. The
autopilot then fills it through the ordinary answer-bank path.

This module is intentionally separate from the resume generator: a motivation
answer is not a resume bullet, and a draft is not a reviewed artifact.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import db, services
from ..config import Config
from ..matching.jd import parse_jd
from ..matching.skills import extract_skills
from ..models import Job, Preferences, Profile, utcnow
from ..tailoring.llm import LLMClient
from ..tailoring.llm_resume import build_source_items
from .answers import SENSITIVE_PATTERN
from ..locking import action_lock

DRAFTS_FILE = "answer_drafts.json"
ANSWERS_FILE = "answers.json"
MAX_DRAFTS = 100
MAX_ANSWER_CHARS = 500
MAX_QUESTION_CHARS = 240
MAX_BASIS = 6
MAX_BASIS_TEXT = 300
DRAFT_STATUSES = frozenset({"draft", "approved", "rejected"})
SENSITIVE_DRAFT_CATEGORIES = frozenset(
    {
        "veteran",
        "disability",
        "gender",
        "race",
        "orientation",
        "clearance",
        "salary",
        "work_authorization",
        "sponsorship",
    }
)

DRAFT_SYSTEM = """You draft one short, truthful application answer for a candidate.
Use only the verified candidate facts provided. Do not invent employers, titles,
dates, degrees, certifications, metrics, URLs, skills, or personal experiences.
You may mention the employer's stated mission or role requirements from the job
description, but do not claim the candidate has experience they do not have.
Write in first person, 50-90 words, at most 500 characters, one plain-text
paragraph. No markdown, no greeting, no signature, no clichés. If the facts are
insufficient for a specific answer, return an empty answer and explain the gap.

Return JSON only:
{"answer": "...", "used_fact_ids": ["exp:0:b:0"], "gap": ""}
"""


class DraftError(ValueError):
    """The request cannot produce a safe draft."""


@dataclass
class AnswerDraft:
    id: str
    question: str
    category: str
    job_id: int
    company: str
    title: str
    text: str
    used_fact_ids: list[str] = field(default_factory=list)
    basis: list[dict[str, str]] = field(default_factory=list)
    model: str = ""
    warnings: list[str] = field(default_factory=list)
    status: str = "draft"
    created_at: str = field(default_factory=utcnow)
    approved_at: str = ""
    approved_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnswerDraft":
        status = str(data.get("status") or "draft")
        if status not in DRAFT_STATUSES:
            status = "draft"
        return cls(
            id=str(data.get("id") or ""),
            question=str(data.get("question") or ""),
            category=str(data.get("category") or "motivation"),
            job_id=int(data.get("job_id") or 0),
            company=str(data.get("company") or ""),
            title=str(data.get("title") or ""),
            text=str(data.get("text") or ""),
            used_fact_ids=[str(item) for item in (data.get("used_fact_ids") or []) if str(item).strip()],
            basis=[
                {"id": str(item.get("id") or ""), "text": str(item.get("text") or "")}
                for item in (data.get("basis") or [])
                if isinstance(item, dict)
            ],
            model=str(data.get("model") or ""),
            warnings=[str(item) for item in (data.get("warnings") or []) if str(item).strip()],
            status=status,
            created_at=str(data.get("created_at") or utcnow()),
            approved_at=str(data.get("approved_at") or ""),
            approved_key=str(data.get("approved_key") or ""),
        )


def drafts_path(config: Config) -> Path:
    return Path(config.data_dir) / DRAFTS_FILE


def answers_path(config: Config) -> Path:
    return Path(config.data_dir) / ANSWERS_FILE


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_drafts(config: Config) -> list[AnswerDraft]:
    path = drafts_path(config)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = payload.get("drafts") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return []
    drafts: list[AnswerDraft] = []
    for item in raw:
        if isinstance(item, dict):
            try:
                drafts.append(AnswerDraft.from_dict(item))
            except (TypeError, ValueError):
                continue
    return drafts


def _save_drafts(config: Config, drafts: list[AnswerDraft]) -> None:
    trimmed = drafts[-MAX_DRAFTS:]
    _write_private(
        drafts_path(config),
        json.dumps(
            {"version": 1, "drafts": [draft.to_dict() for draft in trimmed]},
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
        + "\n",
    )


def _read_answers(config: Config) -> dict[str, str]:
    try:
        payload = json.loads(answers_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items() if isinstance(key, str) and isinstance(value, str)}


def _write_answers(config: Config, overrides: dict[str, str]) -> None:
    _write_private(
        answers_path(config),
        json.dumps(overrides, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
    )


def _canonical_key(question: str) -> str:
    key = re.sub(r"[^a-z0-9]+", " ", str(question or "").lower()).strip()
    return key[:120]


def _clean_answer(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text


def _structural_error(text: str) -> str:
    text = _clean_answer(text)
    if not text:
        return "Answer is empty."
    if len(text) > MAX_ANSWER_CHARS:
        return f"Answer is longer than {MAX_ANSWER_CHARS} characters."
    if any(ch in "<>" or ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return "Answer may not contain '<', '>', or control characters."
    return ""


def _question_allowed(question: str, category: str = "") -> str:
    canonical = _canonical_key(question)
    if not canonical:
        return "Question is empty."
    if len(question) > MAX_QUESTION_CHARS:
        return f"Question is longer than {MAX_QUESTION_CHARS} characters."
    if category not in {"motivation"}:
        return "Only motivation/free-text answers can be drafted."
    if category in SENSITIVE_DRAFT_CATEGORIES or SENSITIVE_PATTERN.search(canonical):
        return "Sensitive or legally attestable questions cannot be drafted; answer them yourself."
    if re.search(r"\b(?:salary|compensation|desired pay|pay expectation)\b", canonical):
        return "Salary questions cannot be drafted; add an approved override yourself."
    return ""


def _candidate_context(profile: Profile) -> tuple[list[dict[str, Any]], str]:
    items = build_source_items(profile)
    facts = [
        {
            "id": item.id,
            "text": item.text[:MAX_BASIS_TEXT],
            "skills": list(item.skills),
            "section": item.section,
        }
        for item in items[:60]
    ]
    summary = " | ".join(
        part
        for part in (
            profile.summary,
            "Skills: " + ", ".join(str(skill) for skill in (profile.skills or [])[:40]),
            "Education: "
            + "; ".join(
                f"{entry.get('degree', '')} {entry.get('field', '')} {entry.get('school', '')}".strip()
                for entry in (profile.education or [])[:3]
            ),
        )
        if part.strip()
    )
    return facts, summary


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\b\d[\d,.]*\b", text or ""))


def _allowed_skills(profile: Profile, facts: list[dict[str, Any]]) -> set[str]:
    allowed = {str(skill) for skill in (profile.skills or []) if str(skill).strip()}
    for item in facts:
        allowed.update(str(skill) for skill in (item.get("skills") or []))
        allowed.update(extract_skills(str(item.get("text") or "")))
    return allowed


def _build_prompt(
    profile: Profile, job: Job, question: str, category: str
) -> tuple[str, list[dict[str, Any]], str]:
    facts, summary = _candidate_context(profile)
    jd = parse_jd(
        job.description or job.title,
        title_hint=job.title,
        company_hint=job.company,
        location_hint=job.location,
    )
    payload = {
        "question": question,
        "category": category,
        "candidate": {
            "name": profile.full_name,
            "summary": summary,
            "skills": list(profile.skills or [])[:40],
        },
        "job": {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "description": (job.description or "")[:8000],
            "must_skills": list(jd.must_skills)[:20],
            "skills": list(jd.skills)[:40],
        },
        "verified_facts": facts,
    }
    return (
        "Draft one application answer from this verified context.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2),
        facts,
        summary,
    )


def draft_answer(
    config: Config,
    conn: Any,
    job_id: int,
    question: str,
    *,
    category: str = "motivation",
    llm: Any | None = None,
) -> AnswerDraft:
    """Generate a draft answer and save it for human review.

    The draft is not written to the answer bank. It is safe to call from a web
    request; provider failures raise ``DraftError`` with a user-facing message.
    """
    blocked = _question_allowed(question, category)
    if blocked:
        raise DraftError(blocked)
    job = db.get_job(conn, int(job_id))
    if job is None:
        raise DraftError(f"Job {job_id} not found.")
    profile, _prefs = services.require_profile(config, conn)
    facts, summary = _candidate_context(profile)
    if not facts:
        raise DraftError("No verified candidate facts are available to ground a draft.")
    prompt, facts, summary = _build_prompt(profile, job, question, category)

    client = llm or LLMClient(config)
    if not getattr(client, "available", False):
        raise DraftError(getattr(client, "error", "") or "Model provider is not configured.")
    try:
        raw = client.json_chat(DRAFT_SYSTEM, prompt, max_tokens=1400)
    except Exception as exc:  # noqa: BLE001 - provider errors are user-facing
        raise DraftError(f"Draft generation failed: {str(exc)[:240]}") from exc
    if not isinstance(raw, dict):
        raise DraftError("The model returned no usable draft JSON.")

    answer = _clean_answer(str(raw.get("answer") or ""))
    gap = _clean_answer(str(raw.get("gap") or ""))
    ids = [str(item) for item in (raw.get("used_fact_ids") or []) if str(item).strip()]
    ids = list(dict.fromkeys(ids))
    by_id = {item["id"]: item for item in facts}
    cited = [by_id[item] for item in ids if item in by_id]
    if not answer:
        raise DraftError(gap or "The model could not draft a grounded answer.")
    structural = _structural_error(answer)
    if structural:
        raise DraftError(structural)
    if not ids:
        raise DraftError("The draft did not cite a verified candidate fact.")
    if len(cited) != len(ids):
        raise DraftError("The draft cited an unknown candidate fact.")
    source_text = " ".join(str(item.get("text") or "") for item in cited)
    # Candidate claims must be grounded in the cited facts, not merely in a
    # number that happens to appear in the employer's job description.
    unsupported_numbers = _numbers(answer) - _numbers(source_text)
    if unsupported_numbers:
        raise DraftError(
            "The draft contained a number not present in the cited facts or job description: "
            + ", ".join(sorted(unsupported_numbers))
        )
    allowed_skills = _allowed_skills(profile, facts)
    unknown_skills = set(extract_skills(answer)) - allowed_skills
    if unknown_skills:
        raise DraftError(
            "The draft claimed a skill not present in the verified profile: "
            + ", ".join(sorted(unknown_skills))
        )

    basis = [
        {"id": item["id"], "text": str(item.get("text") or "")[:MAX_BASIS_TEXT]}
        for item in cited[:MAX_BASIS]
    ]
    draft = AnswerDraft(
        id=uuid.uuid4().hex,
        question=_clean_answer(question),
        category=category,
        job_id=int(job.id or job_id),
        company=job.company or "",
        title=job.title or "",
        text=answer,
        used_fact_ids=ids,
        basis=basis,
        model=str(getattr(client, "model", "") or ""),
        warnings=[gap] if gap else [],
        status="draft",
        created_at=utcnow(),
    )
    with action_lock(config):
        drafts = read_drafts(config)
        drafts.append(draft)
        _save_drafts(config, drafts)
    return draft


def approve_draft(
    config: Config, draft_id: str, *, text: str | None = None
) -> AnswerDraft:
    """Approve a draft and write its text into the answer bank."""
    with action_lock(config):
        drafts = read_drafts(config)
        draft = next((item for item in drafts if item.id == draft_id), None)
        if draft is None:
            raise DraftError("Draft not found.")
        if draft.status != "draft":
            raise DraftError(f"Draft is already {draft.status}.")
        blocked = _question_allowed(draft.question, draft.category)
        if blocked:
            raise DraftError(blocked)
        final_text = _clean_answer(text if text is not None else draft.text)
        structural = _structural_error(final_text)
        if structural:
            raise DraftError(structural)
        key = _canonical_key(draft.question)
        if not key:
            raise DraftError("Draft question has no usable answer-bank key.")
        overrides = _read_answers(config)
        overrides[key] = final_text
        _write_answers(config, overrides)
        draft.text = final_text
        draft.status = "approved"
        draft.approved_at = utcnow()
        draft.approved_key = key
        _save_drafts(config, drafts)
    return draft


def reject_draft(config: Config, draft_id: str) -> AnswerDraft:
    with action_lock(config):
        drafts = read_drafts(config)
        draft = next((item for item in drafts if item.id == draft_id), None)
        if draft is None:
            raise DraftError("Draft not found.")
        if draft.status != "draft":
            raise DraftError(f"Draft is already {draft.status}.")
        draft.status = "rejected"
        _save_drafts(config, drafts)
    return draft


def list_drafts(config: Config, limit: int = 50) -> list[AnswerDraft]:
    drafts = read_drafts(config)
    return list(reversed(drafts[-max(1, min(MAX_DRAFTS, int(limit))) :]))
