"""Resolve candidate years of experience without treating "unset" as zero.

Explicit values win: ``profile.work_auth["years_experience"]`` first, then a
confirmed preference. If neither is present, estimate from non-internship
experience entries with parseable dates. The estimate merges overlapping
intervals and ignores internships so it stays conservative.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable

from ..models import Preferences, Profile

INTERN_TITLE = re.compile(r"\b(intern|internship|co-?op)\b", re.IGNORECASE)
ISO_DATE = re.compile(r"^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$")
MONTH_YEAR = re.compile(r"^(\d{1,2})/(\d{4})$")
NAMED_MONTH = re.compile(r"^([a-z]{3,9})\.?[\s-]+(\d{4})$")
YEAR_ONLY = re.compile(r"^(\d{4})$")
MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass(frozen=True)
class YearsEstimate:
    value: float | None
    source: str  # profile | preferences | experience | unknown


def coerce_years(value: Any) -> float | None:
    """Return a finite non-negative year count, or None when the value is unset."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return round(number, 2)


def _parse_date(value: Any, *, end: bool, today: date) -> date | None:
    text = str(value or "").strip().lower()
    if not text:
        return today if end else None
    if text in {"present", "current", "now", "today"}:
        return today if end else None

    match = ISO_DATE.match(text)
    if match:
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            return None
        return date(year, month, 1)

    match = MONTH_YEAR.match(text)
    if match:
        month, year = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            return None
        return date(year, month, 1)

    match = NAMED_MONTH.match(text)
    if match:
        month = MONTHS.get(match.group(1)[:3])
        if month is None:
            return None
        return date(int(match.group(2)), month, 1)

    match = YEAR_ONLY.match(text)
    if match:
        year = int(match.group(1))
        return date(year, 12 if end else 1, 1)

    return None


def estimate_years(
    entries: Iterable[dict[str, Any]], *, today: date | None = None
) -> float | None:
    """Estimate professional years from dated, non-internship experience entries."""
    current = today or datetime.now(timezone.utc).date()
    intervals: list[tuple[int, int]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if INTERN_TITLE.search(str(entry.get("title") or "")):
            continue
        start = _parse_date(entry.get("start"), end=False, today=current)
        end = _parse_date(entry.get("end"), end=True, today=current)
        if start is None:
            continue
        if end is None:
            end = current
        if start > current:
            continue
        end = min(end, current)
        if end < start:
            continue
        start_index = start.year * 12 + start.month - 1
        end_index = end.year * 12 + end.month - 1
        if end_index < start_index:
            continue
        intervals.append((start_index, end_index))

    if not intervals:
        return None

    intervals.sort()
    merged: list[list[int]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)

    months = sum(end - start for start, end in merged)
    return round(months / 12, 1)


def resolve_years(
    profile: Profile, prefs: Preferences, *, today: date | None = None
) -> YearsEstimate:
    """Resolve years from explicit settings, then dated experience, then unknown."""
    explicit = coerce_years((profile.work_auth or {}).get("years_experience"))
    if explicit is not None:
        return YearsEstimate(explicit, "profile")

    configured = coerce_years(getattr(prefs, "years_of_experience", None))
    if getattr(prefs, "years_experience_confirmed", False) and configured is not None:
        return YearsEstimate(configured, "preferences")
    # Legacy preference rows have no confirmation flag. A positive value is
    # still an explicit signal; a bare 0 is the old default and means unset.
    if configured is not None and configured > 0:
        return YearsEstimate(configured, "preferences")

    estimate = estimate_years(profile.experience, today=today)
    if estimate is not None:
        return YearsEstimate(estimate, "experience")
    return YearsEstimate(None, "unknown")
