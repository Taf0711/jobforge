"""Local referral-network graph and warm-path routing.

The graph is a small, local file (YAML or JSON) listing people the user knows.
It answers two questions: *do I know anyone at this company?* and *how should
that change the channel and priority?*

This module deliberately does not change JobForge's fit score. `modify_fit`
returns a separate, display-only adjustment; callers may choose to show it but
must keep the stored `MatchResult.score` unchanged.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

RELATIONSHIPS = frozenset(
    {
        "alumni",
        "former_colleague",
        "friend",
        "recruiter",
        "hiring_manager",
        "other",
    }
)
STRENGTHS = frozenset({"strong", "medium", "weak"})

STRENGTH_WEIGHT = {"strong": 1.0, "medium": 0.6, "weak": 0.3}
ROLE_BONUS = {"hiring_manager": 0.15, "recruiter": 0.10, "alumni": 0.05}

RECENCY_BONUS_90_DAYS = 0.10
RECENCY_BONUS_365_DAYS = 0.05

COMPANY_SUFFIXES = frozenset(
    {
        "inc",
        "llc",
        "ltd",
        "corp",
        "corporation",
        "co",
        "company",
        "group",
        "holdings",
        "technologies",
        "technology",
        "software",
        "systems",
        "solutions",
        "labs",
    }
)

# Channel thresholds. Shared by `channel` and documented in docs/05-warm-path.md.
REFERRAL_FIT = 70
REFERRAL_WARM = 70
WARM_INTRO_FIT = 50
WARM_INTRO_WARM = 40
FALLBACK_FIT = 40

CHANNELS = frozenset(
    {
        "referral_first",
        "warm_intro",
        "cold_with_warm_fallback",
        "cold_only",
    }
)


def normalize_company(name: str) -> str:
    """Canonicalize a company name for contact matching.

    Lowercases, turns punctuation into spaces, collapses whitespace, and strips
    trailing corporate suffixes (``Inc``, ``LLC``, ``Technologies``, ...) so
    "Acme, Inc." and "Acme Technologies LLC" both become "acme".
    """
    if not isinstance(name, str):
        return ""
    value = name.lower().replace("_", " ")
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    tokens = value.split()
    while len(tokens) > 1 and tokens[-1] in COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclass
class WarmPath:
    """Referral summary for one company.

    ``company`` is the normalized company key. ``score`` is 0-100 and reflects
    the strongest matching contact only. ``contacts`` holds plain dicts so the
    result is JSON-serializable for the CLI/web layer. ``action`` is the
    warm-only recommendation; combine with fit via :func:`channel`.
    """

    company: str
    score: int
    contacts: list[dict[str, str]] = field(default_factory=list)
    reason: str = ""
    action: str = "cold_only"


def _enum_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 10:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _last_contact_text(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return _text(value)


def _role_bonus(contact: dict[str, str]) -> float:
    """Best role/relationship bonus for a contact (never additive)."""
    bonus = 0.0
    for field_name in ("role", "relationship"):
        key = _enum_key(contact.get(field_name, ""))
        if not key:
            continue
        if "hiring" in key and "manager" in key:
            bonus = max(bonus, ROLE_BONUS["hiring_manager"])
        if "recruiter" in key:
            bonus = max(bonus, ROLE_BONUS["recruiter"])
        if key == "alumni":
            bonus = max(bonus, ROLE_BONUS["alumni"])
    return bonus


def _recency_bonus(last_contact: str, today: date) -> float:
    contact_date = _parse_date(last_contact)
    if contact_date is None:
        return 0.0
    days = (today - contact_date).days
    if days <= 90:
        return RECENCY_BONUS_90_DAYS
    if days <= 365:
        return RECENCY_BONUS_365_DAYS
    return 0.0


def _contact_value(contact: dict[str, str], today: date) -> float:
    base = STRENGTH_WEIGHT.get(contact.get("strength", ""), 0.0)
    value = base + _role_bonus(contact) + _recency_bonus(contact.get("last_contact", ""), today)
    return min(1.0, value)


def _action_for_score(score: int) -> str:
    if score >= REFERRAL_WARM:
        return "referral_first"
    if score >= WARM_INTRO_WARM:
        return "warm_intro"
    if score > 0:
        return "cold_with_warm_fallback"
    return "cold_only"


class NetworkGraph:
    """Load a local referral network and rank warm paths.

    Malformed files or entries never raise; they are recorded in ``warnings``
    and skipped so a typo in one contact cannot break the dashboard.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connections: list[dict[str, str]] = []
        self.warnings: list[str] = []
        self._load()

    def _load(self) -> None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            self.warnings.append(f"Could not read network file {self.path}: {exc}")
            return

        try:
            if self.path.suffix.lower() == ".json":
                data = json.loads(text)
            else:
                data = yaml.safe_load(text)
        except (yaml.YAMLError, ValueError) as exc:
            self.warnings.append(f"Could not parse network file {self.path}: {exc}")
            return

        if data is None:
            self.warnings.append(f"Network file {self.path} is empty")
            return
        if not isinstance(data, dict):
            self.warnings.append(f"Network file {self.path} must contain a mapping")
            return

        raw_connections = data.get("connections")
        if raw_connections is None:
            self.warnings.append(f"Network file {self.path} has no 'connections' list")
            return
        if not isinstance(raw_connections, list):
            self.warnings.append("'connections' must be a list")
            return

        for index, item in enumerate(raw_connections):
            contact = self._parse_contact(item, index)
            if contact is not None:
                self.connections.append(contact)

    def _parse_contact(self, item: Any, index: int) -> dict[str, str] | None:
        prefix = f"connections[{index}]"
        if not isinstance(item, dict):
            self.warnings.append(f"{prefix}: expected a mapping, skipped")
            return None

        name = _text(item.get("name"))
        company = _text(item.get("company"))
        if not name:
            self.warnings.append(f"{prefix}: missing 'name', skipped")
            return None
        if not company:
            self.warnings.append(f"{prefix}: missing 'company', skipped")
            return None

        relationship = _enum_key(item.get("relationship"))
        if relationship not in RELATIONSHIPS:
            self.warnings.append(f"{prefix}: invalid 'relationship', skipped")
            return None

        strength = _enum_key(item.get("strength"))
        if strength not in STRENGTHS:
            self.warnings.append(f"{prefix}: invalid 'strength', skipped")
            return None

        last_contact = _last_contact_text(item.get("last_contact"))
        if last_contact and _parse_date(last_contact) is None:
            self.warnings.append(f"{prefix}: invalid 'last_contact', skipped")
            return None

        return {
            "name": name,
            "company": company,
            "role": _text(item.get("role")),
            "relationship": relationship,
            "strength": strength,
            "last_contact": last_contact,
            "notes": _text(item.get("notes")),
        }

    def contacts_for(self, company: str) -> list[dict[str, str]]:
        """Return matching contacts, strongest first and then most recent."""
        canonical = normalize_company(company)
        if not canonical:
            return []
        matches = [
            dict(contact)
            for contact in self.connections
            if normalize_company(contact.get("company", "")) == canonical
        ]

        def sort_key(contact: dict[str, str]) -> tuple[float, int]:
            weight = STRENGTH_WEIGHT.get(contact.get("strength", ""), 0.0)
            contact_date = _parse_date(contact.get("last_contact", ""))
            recency = contact_date.toordinal() if contact_date else 0
            return (-weight, -recency)

        return sorted(matches, key=sort_key)

    def warm_path(self, company: str, today: date | None = None) -> WarmPath:
        """Summarize the best referral path to ``company``.

        The score is the strongest matching contact's value, where
        ``value = strength weight + role bonus + recency bonus`` capped at 1.0
        and multiplied by 100. No matching contacts yields score 0.
        """
        matches = self.contacts_for(company)
        canonical = normalize_company(company)
        if not matches:
            label = canonical or "that company"
            return WarmPath(
                company=canonical,
                score=0,
                contacts=[],
                reason=f"No known contacts at {label}",
                action="cold_only",
            )

        current_day = today or date.today()
        best = max(matches, key=lambda contact: _contact_value(contact, current_day))
        value = _contact_value(best, current_day)
        score = int(round(100 * value))
        label = canonical or best["company"]
        detail = f"best contact: {best['name']} ({best['strength']} {best['relationship']})"
        if best["last_contact"]:
            detail += f", last contact {best['last_contact']}"
        reason = f"{len(matches)} contact(s) at {label}; {detail}"

        return WarmPath(
            company=canonical,
            score=score,
            contacts=matches,
            reason=reason,
            action=_action_for_score(score),
        )


def channel(fit_score: float, warm_path: WarmPath) -> str:
    """Choose an application channel from fit and referral strength.

    Thresholds (also documented in ``docs/05-warm-path.md``):

    * no warm contacts, or fit below 40 -> ``cold_only``
    * fit >= 70 and warm score >= 70 -> ``referral_first``
    * fit >= 50 and warm score >= 40 -> ``warm_intro``
    * fit >= 40 and any warm contact -> ``cold_with_warm_fallback``
    * otherwise -> ``cold_only``

    A referral cannot rescue a role below the 40-point fit floor; the caller
    should not spend social capital on a poor match.
    """
    fit = float(fit_score)
    warm = int(warm_path.score)
    if warm <= 0 or fit < FALLBACK_FIT:
        return "cold_only"
    if fit >= REFERRAL_FIT and warm >= REFERRAL_WARM:
        return "referral_first"
    if fit >= WARM_INTRO_FIT and warm >= WARM_INTRO_WARM:
        return "warm_intro"
    return "cold_with_warm_fallback"


def modify_fit(fit_score: float, warm_path: WarmPath) -> tuple[float, float, str]:
    """Return a display-only fit adjustment for a warm path.

    ``adjusted = min(100, fit + 0.15 * warm_score)`` and ``delta`` is capped at
    +15. The stored fit score must not be overwritten with this value.

    Weak contacts therefore add little: a 30-point warm path moves a 35 fit to
    39.5, not into a high band.
    """
    fit = float(fit_score)
    warm = int(warm_path.score)
    if warm <= 0:
        return fit, 0.0, "No warm path; fit unchanged."

    raw = fit + 0.15 * warm
    adjusted = min(100.0, raw)
    delta = adjusted - fit
    if delta > 15.0:
        delta = 15.0
        adjusted = fit + delta

    adjusted = round(adjusted, 6)
    delta = round(adjusted - fit, 6)
    reason = f"Warm path score {warm} adds {delta:+.1f} fit points"
    if raw > 100.0:
        reason += " (capped at 100)"
    return adjusted, delta, reason
