"""Multiplicative hard-gate survival probabilities.

Adapted from the resume-forge scoring model. These are pure functions: each
returns a survival probability in [0, 1] for one gate, so callers multiply them
into the fit score instead of applying flat deductions.
"""

from __future__ import annotations


def citizenship_survival(required: bool, is_citizen: bool) -> float:
    if not required:
        return 1.0
    return 1.0 if is_citizen else 0.02


def itar_survival(required: bool, eligible: bool) -> float:
    if not required:
        return 1.0
    return 1.0 if eligible else 0.05


def clearance_survival(active_required: bool, eligible: bool = True) -> float:
    """A current-clearance requirement is a real obstacle for an eligible
    candidate who does not hold one; an ineligible candidate is effectively out."""
    if active_required:
        return 0.35 if eligible else 0.02
    return 1.0


def gpa_survival(floor: float | None, candidate: float | None) -> float:
    if floor is None or candidate is None:
        return 1.0
    if candidate >= floor:
        return 1.0
    gap = floor - candidate
    if gap <= 0.2:
        return 0.5
    return 0.08
