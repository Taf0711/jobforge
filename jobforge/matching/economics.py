"""Compensation policy and location economics.

Adapted from the resume-forge scorer. These functions return *Pursuit*
modifiers; they never change the stored Fit score. NYC has a two-tier floor:
$130k target, $105k exception only when the funnel looks winnable, below $105k
is a skip. Relocation economics uses a small metro cost-of-living table.
"""

from __future__ import annotations

import re
from typing import Any, Optional

NYC_MARKERS = (
    "new york", "nyc", "manhattan", "brooklyn", "queens", "bronx",
    "staten island", "jersey city", "hoboken", "newark",
)

DEFAULT_COMP_POLICY: dict[str, Any] = {
    "nyc_target_floor": 130_000,
    "nyc_exception_floor": 105_000,
    "exception_min_callback_pct": 25.0,
    "warm_channels": [
        "referral", "recruiter_dm", "alumni", "event", "direct_email",
        "linkedin_easy_apply",
    ],
    "modifiers": {
        "meets_target": 2,
        "exception_ok": 0,
        "exception_unlikely": -12,
        "below_floor": -20,
        "verify_comp": -5,
        "not_applicable": 0,
    },
}

KNOWN_METRO_COL: dict[str, int] = {
    "ashburn": 149, "loudoun": 149, "reston": 149, "herndon": 149,
    "arlington": 149, "mclean": 149, "tysons": 149, "fairfax": 149,
    "washington": 145, "dc metro": 145, "district of columbia": 145,
    "san francisco": 170, "bay area": 160, "silicon valley": 170,
    "san jose": 165, "palo alto": 170, "mountain view": 170, "menlo park": 170,
    "new york": 155, "nyc": 155, "manhattan": 155, "brooklyn": 155, "queens": 155,
    "seattle": 140, "los angeles": 140, "boston": 145, "cambridge": 145,
    "miami": 120,
    "reno": 109, "durham": 98, "raleigh": 98, "charlotte": 98, "memphis": 92,
    "kansas city": 95, "columbus": 95, "cincinnati": 95, "huntsville": 95,
    "montgomery": 92, "birmingham": 92, "tampa": 98, "st. louis": 95,
    "buffalo": 98, "rochester": 95, "cleveland": 95, "pittsburgh": 100,
    "dallas": 112, "houston": 108, "nashville": 110, "omaha": 95, "des moines": 95,
    "springfield": 95, "quincy": 95,
    "austin": 112, "chicago": 115, "atlanta": 110, "philadelphia": 108,
    "denver": 115, "boulder": 130, "frisco": 110, "plano": 112, "irving": 110,
}
LOW_COL_MAX = 110
HIGH_COL_MIN = 128
JUNIOR_NORMALIZED_FLOOR = 55_000

_SALARY_RE = re.compile(r"\$\s?(\d{2,3})(?:,(\d{3}))?\s?(k)?", re.IGNORECASE)


def is_nyc_role(location_text: str) -> bool:
    low = (location_text or "").lower()
    return any(marker in low for marker in NYC_MARKERS)


def parse_salary_range(text: str) -> tuple[Optional[float], Optional[float]]:
    values: list[float] = []
    for match in _SALARY_RE.finditer(text or ""):
        whole, thousands, k = match.group(1), match.group(2), match.group(3)
        if thousands:
            value = float(whole + thousands)
        elif k:
            value = float(whole) * 1000
        else:
            continue
        if 40_000 <= value <= 600_000:
            values.append(value)
    if not values:
        return None, None
    return min(values), max(values)


def low_or(value: Optional[float], fallback: float) -> float:
    return float(value) if value is not None else float(fallback)


def comp_policy(
    is_nyc: bool,
    salary_low: Optional[float],
    salary_high: Optional[float],
    channel: Optional[str] = None,
    callback_pct: float = 0.0,
    policy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    p = dict(DEFAULT_COMP_POLICY)
    p.update(policy or {})
    mods = p["modifiers"]
    high = salary_high if salary_high is not None else salary_low

    if not is_nyc:
        return {
            "verdict": "not_applicable",
            "modifier": mods["not_applicable"],
            "salary_low": salary_low,
            "salary_high": salary_high,
            "chances_good": None,
            "notes": ["not an NYC-metro role — cost-of-living logic applies instead"],
        }

    warm = bool(channel and channel in p["warm_channels"])
    odds_ok = callback_pct >= p["exception_min_callback_pct"]
    chances_good = warm or odds_ok
    odds_bits = []
    if warm:
        odds_bits.append(f"warm channel {channel}")
    if odds_ok:
        odds_bits.append(f"callback {callback_pct:.0f}% >= {p['exception_min_callback_pct']:.0f}%")
    odds_text = " and ".join(odds_bits) if odds_bits else f"cold odds (callback {callback_pct:.0f}%)"

    if high is None:
        return {
            "verdict": "verify_comp",
            "modifier": mods["verify_comp"],
            "salary_low": None,
            "salary_high": None,
            "chances_good": chances_good,
            "notes": [
                f"NYC role with no posted band — confirm >= ${p['nyc_target_floor']:,.0f} target "
                f"(exception floor ${p['nyc_exception_floor']:,.0f}) before submitting"
            ],
        }

    band = f"${salary_low:,.0f}-${salary_high:,.0f}" if salary_low != salary_high else f"${high:,.0f}"
    if high >= p["nyc_target_floor"]:
        return {
            "verdict": "meets_target",
            "modifier": mods["meets_target"],
            "salary_low": low_or(salary_low, high),
            "salary_high": high,
            "chances_good": chances_good,
            "notes": [f"{band} clears the ${p['nyc_target_floor']:,.0f} NYC target floor"],
        }
    if high >= p["nyc_exception_floor"]:
        if chances_good:
            return {
                "verdict": "exception_ok",
                "modifier": mods["exception_ok"],
                "salary_low": low_or(salary_low, high),
                "salary_high": high,
                "chances_good": True,
                "notes": [
                    f"{band} is below the ${p['nyc_target_floor']:,.0f} target but above the "
                    f"${p['nyc_exception_floor']:,.0f} exception floor, with {odds_text}"
                ],
            }
        return {
            "verdict": "exception_unlikely",
            "modifier": mods["exception_unlikely"],
            "salary_low": low_or(salary_low, high),
            "salary_high": high,
            "chances_good": False,
            "notes": [
                f"{band} sits in the ${p['nyc_exception_floor']:,.0f}-${p['nyc_target_floor']:,.0f} "
                f"exception band but {odds_text} — deprioritize unless a warm path appears"
            ],
        }
    return {
        "verdict": "below_floor",
        "modifier": mods["below_floor"],
        "salary_low": low_or(salary_low, high),
        "salary_high": high,
        "chances_good": chances_good,
        "notes": [
            f"{band} is below the ${p['nyc_exception_floor']:,.0f} NYC hard floor — skip"
        ],
    }


def location_economics(
    location: str, salary_mid: Optional[float], location_fit: float
) -> tuple[int, list[str]]:
    if location_fit >= 0.95:
        return 0, []
    loc = (location or "").lower()
    col = 0
    for key, value in KNOWN_METRO_COL.items():
        if key in loc:
            col = value
            break
    if col == 0:
        return 0, []

    if col <= LOW_COL_MAX:
        mod = 2 if salary_mid else 1
        tag = f"{'+' if mod >= 0 else ''}{mod} location economics: low COL ({col}); "
        tag += "listed comp goes further" if salary_mid else "budget-friendly metro"
        return mod, [tag]

    if col >= HIGH_COL_MIN:
        if salary_mid:
            normalized = salary_mid / (col / 100.0)
            if normalized >= JUNIOR_NORMALIZED_FLOOR:
                return -2, [
                    f"-2 location economics: high COL ({col}) but comp covers it "
                    f"(${salary_mid:,.0f} ≈ ${normalized:,.0f} national terms)"
                ]
            return -4, [
                f"-4 location economics: high COL ({col}); salary ${salary_mid:,.0f} "
                f"≈ ${normalized:,.0f} national terms — thin net after relocation"
            ]
        return -4, [
            f"-4 location economics: high COL ({col}) and no salary info — assume thin"
        ]

    return 0, []
