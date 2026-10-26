"""Answer bank for common screening questions.

Sensitive voluntary disclosures are never guessed. If the profile does not
contain an explicit answer, the field is left for the human.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Preferences, Profile

# Overrides are convenient for ordinary screening questions, but they must not
# answer legal attestations or voluntary disclosures. Those come only from the
# profile (or stay unresolved).
SENSITIVE_PATTERN = re.compile(
    r"\b(?:veteran|disabilit\w*|gender|sex|race|ethnic\w*|age|dob)\b"
    r"|\bsexual orientation\b"
    r"|\b(?:authorized|eligible|legally able|permitted|allowed)\b.*\b(?:work|employment)\b"
    r"|\b(?:authorization|right) to work\b|\bwork[- ]authorized\b"
    r"|\b(?:hispanic|latino|latina)\b|\bnational origin\b|\bcitizen\w*\b"
    r"|\b(?:date of birth|birth date|military|marital status)\b"
    r"|\bwork authorization\b|\bsponsor\w*\b|\bclearance\b",
    re.IGNORECASE,
)


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"yes", "true", "y", "1"}:
            return True
        if lowered in {"no", "false", "n", "0"}:
            return False
    return None


@dataclass
class Answer:
    value: str
    category: str
    confidence: str  # high | medium | none


class AnswerBank:
    def __init__(self, path: str | Path | None = None) -> None:
        self.overrides: dict[str, str] = {}
        if path and Path(path).exists():
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.overrides = {
                        re.sub(r"[^a-z0-9]+", " ", str(k).lower()).strip(): str(v)
                        for k, v in data.items()
                    }
            except (ValueError, OSError):
                self.overrides = {}

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.overrides, indent=2), encoding="utf-8")

    def answer(self, question: str, profile: Profile, prefs: Preferences) -> Answer:
        q = re.sub(r"\s+", " ", (question or "").lower()).strip()
        if not q:
            return Answer("", "empty", "none")
        # Field names often use underscores/hyphens (work_authorization).
        # Canonicalize separators before matching sensitive rules so a
        # one-character override cannot bypass the gate.
        canonical = re.sub(r"[^a-z0-9]+", " ", q).strip()

        if not SENSITIVE_PATTERN.search(canonical):
            for key, value in self.overrides.items():
                if key in canonical:
                    return Answer(value, "override", "high")

        rules: list[tuple[str, str, str]] = [
            (r"first name", profile.first_name, "first_name"),
            (r"last name|surname|family name", profile.last_name, "last_name"),
            (r"full name|your name|^name$", profile.full_name, "full_name"),
            (r"e-?mail", profile.email, "email"),
            (r"phone|mobile|telephone", profile.phone, "phone"),
            (r"linkedin", profile.links.get("linkedin", ""), "linkedin"),
            (r"github", profile.links.get("github", ""), "github"),
            (r"portfolio|personal (web)?site|website", profile.links.get("portfolio", ""), "portfolio"),
            (r"city|where.*(based|located)|current location|location", profile.location, "location"),
        ]
        for pattern, value, category in rules:
            if re.search(pattern, q) and value:
                return Answer(value, category, "high")

        if re.search(
            r"(authorized|eligible|legally able|permitted|allowed).*(work|employment)"
            r"|work authorization|authorization to work|right to work|work[- ]authorized",
            canonical,
        ):
            authorized = _as_bool(profile.work_auth.get("authorized"))
            if authorized is not None:
                return Answer("Yes" if authorized else "No", "work_authorization", "high")
            return Answer("", "work_authorization", "none")
        if re.search(r"(require|need).*(sponsor|visa)|sponsorship", canonical):
            value = None
            for key in ("needs_sponsorship", "requires_sponsorship", "sponsorship"):
                if key in profile.work_auth:
                    value = _as_bool(profile.work_auth.get(key))
                    if value is not None:
                        break
            if value is None:
                value = prefs.needs_sponsorship
            return Answer("Yes" if value else "No", "sponsorship", "high")
        if re.search(r"current (company|employer)|present employer", q):
            value = str((profile.experience[0] if profile.experience else {}).get("company", "") or "")
            return Answer(value, "current_company", "medium" if value else "none")
        if re.search(r"current (title|role)|present title", q):
            value = str((profile.experience[0] if profile.experience else {}).get("title", "") or "")
            return Answer(value, "current_title", "medium" if value else "none")
        if re.search(r"salary|compensation|pay (expectation|requirement)|desired pay", q):
            if prefs.salary_min:
                return Answer(str(prefs.salary_min), "salary", "medium")
            return Answer("", "salary", "none")
        if re.search(r"notice period|how soon.*start|available.*start|start date", q):
            return Answer("2 weeks", "start_date", "medium")
        if re.search(r"relocat", q):
            return Answer("Yes" if prefs.open_to_relocation else "No", "relocation", "high")
        if re.search(r"clearance", canonical):
            value = str(profile.work_auth.get("clearance") or "").strip()
            if value:
                return Answer(value, "clearance", "high")
            return Answer("", "clearance", "none")
        if re.search(r"veteran", canonical):
            value = str(profile.disclosures.get("veteran", "") or "")
            return Answer(value, "veteran", "high" if value else "none")
        if re.search(r"disabilit", canonical):
            value = str(profile.disclosures.get("disability", "") or "")
            return Answer(value, "disability", "high" if value else "none")
        if re.search(r"gender", canonical):
            value = str(profile.disclosures.get("gender", "") or "")
            return Answer(value, "gender", "high" if value else "none")
        if re.search(r"race|ethnic", canonical):
            value = str(profile.disclosures.get("race", "") or "")
            return Answer(value, "race", "high" if value else "none")
        if re.search(r"sexual orientation", canonical):
            value = str(profile.disclosures.get("orientation", "") or "")
            return Answer(value, "orientation", "high" if value else "none")
        if re.search(r"why (do you|are you).*(want|interested)|why this (role|company)", q):
            return Answer("", "motivation", "none")
        if re.search(r"how did you hear", q):
            return Answer("Company careers page", "source", "medium")
        return Answer("", "unknown", "none")

    def to_dict(self) -> dict[str, Any]:
        return dict(self.overrides)
