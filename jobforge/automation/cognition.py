"""Deterministic form-question classification for review-gated filling.

The classifier routes a question to a profile/answer-bank category. It never
invents free-text answers. Required questions that need human judgment are
returned as ``review`` so autopilot can stop before Submit and show the gap.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..models import Preferences, Profile
from .answers import AnswerBank

SENSITIVE_CATEGORIES = {
    "veteran",
    "disability",
    "gender",
    "race",
    "orientation",
    "clearance",
    "salary",
}
REVIEW_CATEGORIES = {"motivation", "unknown"}


@dataclass
class QuestionDecision:
    label: str
    category: str
    action: str  # fill | review | skip
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "label": self.label,
            "category": self.category,
            "action": self.action,
            "reason": self.reason,
        }


def _clean_label(label: str) -> str:
    return re.sub(r"\s*\(required[^)]*\)\s*$", "", str(label or "")).strip()


def classify_question(
    label: str,
    profile: Profile,
    prefs: Preferences,
    bank: AnswerBank,
) -> QuestionDecision:
    clean = _clean_label(label)
    answer = bank.answer(clean, profile, prefs)
    category = answer.category or "unknown"
    if category == "unknown" and re.match(r"^why\b|^tell us\b", clean, re.IGNORECASE):
        category = "motivation"
    if answer.value:
        return QuestionDecision(clean, category, "fill", "profile or answer bank")
    if category in SENSITIVE_CATEGORIES:
        return QuestionDecision(
            clean, category, "review", "sensitive disclosure; never guessed"
        )
    if category in REVIEW_CATEGORIES:
        return QuestionDecision(
            clean, category, "review", "free-text answer needs approval"
        )
    return QuestionDecision(clean, category, "skip", "no grounded answer")


def classify_missing(
    labels: list[str],
    profile: Profile,
    prefs: Preferences,
    bank: AnswerBank,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for label in labels:
        clean = _clean_label(label)
        if not clean or clean.lower() in seen:
            continue
        seen.add(clean.lower())
        out.append(classify_question(clean, profile, prefs, bank).to_dict())
    return out
