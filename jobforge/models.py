"""Domain models shared by the CLI, web app, and services."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slugify(value: str, max_len: int = 80) -> str:
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
    return (value[:max_len].rstrip("-") or "item")


class ApplicationStatus(str, Enum):
    DRAFT = "draft"
    PREPARED = "prepared"
    SUBMITTED = "submitted"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    FAILED = "failed"
    WITHDRAWN = "withdrawn"


class EmailCategory(str, Enum):
    VERIFICATION = "verification"
    CONFIRMATION = "confirmation"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTION = "rejection"
    OTHER = "other"


class RunStatus(str, Enum):
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


@dataclass
class Job:
    source: str
    source_id: str
    company: str
    title: str
    location: str = ""
    remote: bool = False
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str = "USD"
    url: str = ""
    apply_url: str = ""
    description: str = ""
    posted_at: str | None = None
    discovered_at: str = field(default_factory=utcnow)
    raw: dict[str, Any] = field(default_factory=dict)
    id: int | None = None
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            basis = "|".join(
                [
                    (self.company or "").strip().lower(),
                    (self.title or "").strip().lower(),
                    (self.location or "").strip().lower(),
                    (self.apply_url or self.url or "").strip().lower(),
                ]
            )
            self.fingerprint = hashlib.sha256(basis.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Profile:
    id: int | None = None
    name: str = "default"
    is_active: bool = False
    resume_path: str = ""
    resume_text: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""
    headline: str = ""
    summary: str = ""
    links: dict[str, str] = field(default_factory=dict)
    experience: list[dict[str, Any]] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    projects: list[dict[str, Any]] = field(default_factory=list)
    certifications: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    work_auth: dict[str, Any] = field(default_factory=dict)
    disclosures: dict[str, Any] = field(default_factory=dict)
    facts: list[dict[str, Any]] = field(default_factory=list)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Profile":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class Preferences:
    titles: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    work_modes: list[str] = field(default_factory=list)
    employment_types: list[str] = field(default_factory=list)
    experience_levels: list[str] = field(default_factory=list)
    years_of_experience: float = 0
    salary_min: int = 0
    salary_max: int | None = None
    needs_sponsorship: bool = False
    open_to_relocation: bool = False
    security_clearance: str = "none"
    excluded_companies: list[str] = field(default_factory=list)
    excluded_keywords: list[str] = field(default_factory=list)
    included_keywords: list[str] = field(default_factory=list)
    min_match: int = 55
    one_application_per_company: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Preferences":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class MatchResult:
    job_id: int | None
    profile_id: int
    score: float
    coverage: float
    gate_survival: float
    reasons: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    requirements: list[dict[str, Any]] = field(default_factory=list)
    model_version: str = "jobforge-v1"

    @property
    def band(self) -> str:
        if self.score >= 80:
            return "strong"
        if self.score >= 65:
            return "good"
        if self.score >= 50:
            return "stretch"
        return "weak"


@dataclass
class Application:
    id: int | None = None
    job_id: int | None = None
    profile_id: int | None = None
    status: str = ApplicationStatus.DRAFT.value
    ats: str = ""
    resume_path: str = ""
    cover_letter_path: str = ""
    answers: dict[str, Any] = field(default_factory=dict)
    screenshot_path: str = ""
    error_code: str = ""
    notes: str = ""
    created_at: str = field(default_factory=utcnow)
    submitted_at: str | None = None
    updated_at: str = field(default_factory=utcnow)


@dataclass
class EmailMessage:
    id: int | None = None
    application_id: int | None = None
    message_id: str = ""
    sender: str = ""
    subject: str = ""
    body: str = ""
    received_at: str = ""
    category: str = EmailCategory.OTHER.value
    verification_code: str = ""
    forwarded: bool = False
