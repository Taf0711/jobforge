"""Fast scan scoring and first-pass filters.

This is the discovery layer, not the deep fit. It answers one question: which
postings are worth opening and deep-scoring? The rubric intentionally uses only
the data a scan already has: title, company, location, source, age, and the
candidate's preferences/history. Deep fit remains in ``matching.score``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

from ..matching.archetypes import detect_archetype
from ..matching.experience import resolve_years
from ..matching.jd import JD, mentions_years, parse_jd
from ..matching.skills import extract_skills
from ..models import Job, Preferences, Profile, ScanResult

# ---------------------------------------------------------------------------
# Normalization and text matching
# ---------------------------------------------------------------------------

US_STATES = set(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC".split()
)
US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine",
    "maryland", "massachusetts", "michigan", "minnesota", "mississippi",
    "missouri", "montana", "nebraska", "nevada", "new hampshire", "new jersey",
    "new mexico", "new york", "north carolina", "north dakota", "ohio",
    "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia",
    "washington", "west virginia", "wisconsin", "wyoming", "district of columbia",
)
NON_US_MARKERS = (
    "canada", "toronto", "vancouver", "ontario", "quebec", "uk",
    "united kingdom", "london", "bristol", "england", "scotland", "ireland",
    "dublin", "germany", "france", "spain", "portugal", "netherlands",
    "sweden", "switzerland", "israel", "india", "bangalore", "bengaluru",
    "hyderabad", "pune", "chennai", "mumbai", "delhi", "gurgaon", "noida",
    "singapore", "japan", "tokyo", "korea", "china", "australia",
    "new zealand", "europe", "emea", "apac", "mexico", "brazil",
    "argentina", "dubai", "uae", "south africa", "nigeria", "kenya",
    "pakistan", "bangladesh", "sri lanka", "nepal", "colombia", "costa rica",
    "philippines", "poland", "warsaw", "krakow", "romania", "buenos aires",
    "sao paulo",
)
MAJOR_HUBS = (
    "san francisco", "bay area", "seattle", "austin", "atlanta", "washington",
    "arlington", "mclean", "reston", "new york", "nyc", "boston", "cambridge",
    "los angeles", "san diego", "san jose", "mountain view", "palo alto",
    "menlo park", "bellevue", "denver", "boulder", "chicago", "philadelphia",
    "miami", "dallas", "houston", "raleigh", "durham", "charlotte",
)
SENIOR = re.compile(
    r"\b(senior|sr\.?|staff|principal|lead|manager|director|head|vp)\b",
    re.IGNORECASE,
)
INTERN = re.compile(r"\b(intern|internship|co-op|coop)\b", re.IGNORECASE)
NEW_GRAD = re.compile(
    r"\b(new grad|new graduate|university grad|entry[-\s]?level|"
    r"engineer\s*(?:i|1)\b|associate|junior|grad(?:uate)?)\b",
    re.IGNORECASE,
)
SWE_TITLE = re.compile(
    r"\b(software engineer|software development engineer|swe|full[-\s]?stack|"
    r"backend|back[-\s]?end|frontend|front[-\s]?end|platform engineer|"
    r"infrastructure engineer|data engineer|machine learning engineer|"
    r"ai engineer|member of technical staff|developer|research scientist)\b",
    re.IGNORECASE,
)
NON_SWE = re.compile(
    r"\b(firmware|hardware|sdet|qa[-\s]?only|quality assurance|data analyst|"
    r"it support|help desk|desktop support|customer|operations|counsel|talent|"
    r"recruiter|marketing|finance|accounting|sales|solutions|product design|"
    r"designer|strategy|fraud|partnerships)\b",
    re.IGNORECASE,
)
LEVEL_KEYWORDS = {"new grad", "new graduate", "entry level", "junior", "intern", "internship"}
KNOWN_STACK = re.compile(
    r"\b(python|java|javascript|typescript|react|next\.js|c\+\+|c#|rust|go|"
    r"aws|gcp|azure|docker|kubernetes|sql|security|cloud|ml|ai|llm|pytorch)\b",
    re.IGNORECASE,
)

COMPETITION_DEFAULT: dict[str, str] = {
    "github_jobs": "mass_aggregator",
    "github_pipe": "mass_aggregator",
    "greenhouse": "direct_ats",
    "lever": "direct_ats",
    "ashby": "direct_ats",
    "workable": "direct_ats",
    "smartrecruiters": "direct_ats",
    "workday": "public_sector",
    "nyc_opendata": "public_sector",
    "usajobs": "public_sector",
    "hn": "community",
}
COMPETITION_BOOST = {
    "public_sector": 4,
    "cleared": 4,
    "direct_ats": 3,
    "community": 2,
    "mass_aggregator": 0,
}
# Years is a ranking signal in scan, not a hard knockout. Caps prevent a role
# that is clearly over-level from presenting as "strong" before deep scoring.
YEARS_SCAN_CAPS = {
    "far_short": 35.0,
    "short": 50.0,
    "close": 70.0,
    "unknown": 70.0,
    "unparsed": 70.0,
}
MARQUEE_COMPANIES = (
    "google", "meta", "facebook", "apple", "amazon", "microsoft", "netflix",
    "openai", "anthropic", "nvidia", "tesla", "stripe",
)
FINANCE_MARKERS = (
    "jpm", "jpmorgan", "goldman", "morgan stanley", "citigroup", "citi",
    "bank", "fintech", "coinbase", "stripe", "robinhood", "brex", "chime",
    "affirm", "adyen", "sofi", "toast", "blackrock", "bloomberg", "capital one",
)
PUBLIC_EDU_MARKERS = (
    "hunter", "cuny", "nyc", "city of", "government", "public sector",
    "education", "drupal", "nonprofit", "university",
)
AI_STARTUP_MARKERS = (
    "ai", "ml", "llm", "agent", "anthropic", "openai", "sierra", "perplexity",
    "cohere", "cursor", "modal", "fireworks", "decagon", "elevenlabs",
    "browserbase", "cognition", "baseten", "databricks", "scale ai", "scaleai",
)
TECH_MARKERS = (
    "security", "cloud", "data", "platform", "infrastructure", "enterprise",
)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def company_key(value: str) -> str:
    text = normalize_text(value)
    text = re.sub(r"\b(inc|llc|corp|corporation|company|co|ltd|limited|group|holdings)\b", " ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def title_key(value: str) -> str:
    text = normalize_text(value)
    text = re.sub(r"\b(senior|sr|staff|principal|lead|manager|director|head|vp)\b", " ", text)
    return re.sub(r"[^a-z0-9+#.]+", " ", text).strip()


def _tokens(value: str) -> set[str]:
    return {token for token in title_key(value).split() if len(token) > 2}


def _similar_title(left: str, right: str) -> bool:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return False
    return len(a & b) / max(1, max(len(a), len(b))) >= 0.6


def _similar_company(left: str, right: str) -> bool:
    a, b = company_key(left), company_key(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    at, bt = set(a.split()), set(b.split())
    return bool(at & bt)


def _parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def age_days(job: Job) -> int | None:
    posted = _parse_dt(job.posted_at)
    if posted is None:
        raw_age = (job.raw or {}).get("age") or (job.raw or {}).get("age_tag")
        if raw_age:
            match = re.search(r"(\d+)\s*(h|d|w|mo|y)", str(raw_age), re.IGNORECASE)
            if match:
                number = int(match.group(1))
                unit = match.group(2).lower()
                return number * {"h": 0, "d": 1, "w": 7, "mo": 30, "y": 365}[unit]
        return None
    return max(0, (datetime.now(timezone.utc) - posted).days)


def is_remote(job: Job) -> bool:
    location = normalize_text(job.location)
    return "remote" in location or bool(job.remote)


def is_us_location(location: str, remote: bool = False) -> bool:
    raw = (location or "").strip()
    text = normalize_text(raw)
    if any(marker in text for marker in ("usa", "united states", "u.s.")):
        return True
    if re.search(r"(?:^|[\s,;|])(?:us)(?:$|[\s,;|])", text):
        return True
    # State codes are case-sensitive and delimiter-bound. A lowercase "in" in
    # "Remote in Canada" is English, not Indiana.
    if re.search(
        rf"(?:^|[\s,;|])(?:{'|'.join(sorted(US_STATES))})(?:$|[\s,;|])",
        raw,
    ):
        return True
    if re.search(r",\s*(?:[a-z]{2})(?:\s*$|\s*[;,|])", text):
        state = re.search(r",\s*([a-z]{2})(?:\s*$|\s*[;,|])", text)
        if state and state.group(1).upper() in US_STATES:
            return True
    if any(name in text for name in US_STATE_NAMES):
        return True
    if any(marker in text for marker in NON_US_MARKERS):
        return False
    # Empty/unknown locations are kept; confidence handles the uncertainty.
    return True


def competition_profile(job: Job) -> str:
    if job.competition_profile:
        return job.competition_profile
    raw = (job.raw or {}).get("competition_profile")
    if raw:
        return str(raw)
    return COMPETITION_DEFAULT.get(job.source, "mass_aggregator")


# ---------------------------------------------------------------------------
# First-pass filters
# ---------------------------------------------------------------------------


def filter_job(
    job: Job,
    *,
    prefs: Preferences,
    applications: Iterable[dict[str, Any]],
    scan_cfg: dict[str, Any],
) -> tuple[bool, str]:
    """Return (keep, reason). Filtered jobs are not inserted by default."""
    cfg = scan_cfg or {}
    max_age = int(cfg.get("max_age_days", 30) or 0)
    age = age_days(job)
    if max_age and age is not None and age > max_age:
        return False, f"stale ({age}d)"

    if bool(cfg.get("exclude_non_us", True)) and not is_us_location(job.location, is_remote(job)):
        return False, "non-US location"

    title = job.title or ""
    company = job.company or ""
    text = f"{title} {company}".lower()

    if INTERN.search(title):
        levels = {str(item).lower() for item in prefs.experience_levels}
        wants_intern = any("intern" in item for item in levels)
        if bool(cfg.get("exclude_internships", True)) and not wants_intern:
            return False, "internship outside target levels"

    if SENIOR.search(title) and bool(cfg.get("exclude_senior", True)):
        return False, "senior/lead title"

    if NON_SWE.search(text) and not SWE_TITLE.search(title):
        return False, "non-SWE title"

    keywords = [str(item).lower() for item in (cfg.get("keywords") or []) if str(item).strip()]
    keywords.extend(str(item).lower() for item in prefs.titles if str(item).strip())
    technical_keywords = [item for item in keywords if item not in LEVEL_KEYWORDS]
    exclude = [str(item).lower() for item in (cfg.get("exclude_keywords") or []) if str(item).strip()]
    exclude.extend(str(item).lower() for item in prefs.excluded_keywords if str(item).strip())
    if any(item in text for item in exclude):
        return False, "excluded keyword"
    if prefs.excluded_companies and any(_similar_company(company, item) for item in prefs.excluded_companies):
        return False, "excluded company"

    target_title = bool(SWE_TITLE.search(title)) or any(
        keyword in text for keyword in technical_keywords
    )
    if not target_title:
        return False, "title outside target families"

    rejection_days = int(cfg.get("recent_rejection_days", 60) or 0)
    now = datetime.now(timezone.utc)
    for app in applications or []:
        status = str(app.get("status") or "").lower()
        if status not in {"submitted", "screening", "interview", "offer", "rejected"}:
            continue
        if not _similar_company(company, str(app.get("company") or "")):
            continue
        same_role = _similar_title(title, str(app.get("title") or ""))
        if status in {"submitted", "screening", "interview", "offer"} and same_role:
            return False, "already applied"
        if status == "rejected":
            updated = _parse_dt(app.get("updated_at") or app.get("submitted_at"))
            if same_role:
                return False, "already applied (rejected)"
            if rejection_days and updated is not None:
                if (now - updated).days <= rejection_days:
                    return False, "recent rejection at company"
    return True, ""


# ---------------------------------------------------------------------------
# Quick scan rubric
# ---------------------------------------------------------------------------


def _level_score(title: str) -> tuple[int, str]:
    if NEW_GRAD.search(title):
        return 25, "new grad / entry / engineer 1"
    if re.search(r"\b(associate|junior)\b", title, re.IGNORECASE):
        return 20, "associate / junior"
    if SWE_TITLE.search(title):
        return 15, "generic SWE title"
    return 8, "ambiguous level"


def _relevance_score(
    job: Job, archetypes: dict[str, dict[str, Any]]
) -> tuple[int, str, str]:
    text = f"{job.title}\n{job.company}\n{(job.description or '')[:2000]}"
    match = detect_archetype(JD(title=job.title, description=text), archetypes or {})
    name = match.name if match else ""
    mapping = {
        "backend_platform": (25, "backend / platform"),
        "fullstack_product": (24, "full-stack / product"),
        "cloud_devops_automation": (24, "cloud / automation"),
        "security_identity_compliance": (24, "security / identity"),
        "frontend_web": (21, "frontend / web"),
        "ai_llm": (22, "AI / LLM"),
        "ml_computer_vision": (20, "ML / computer vision"),
        "systems_networking_embedded": (20, "systems / networking"),
        "government_defense_regulated": (20, "government / defense"),
        "it_support_infrastructure": (5, "IT support"),
    }
    if name in mapping:
        score, label = mapping[name]
        return score, label, name
    lowered = text.lower()
    if re.search(r"\b(ai|ml|machine learning|llm|genai)\b", lowered):
        return 20, "AI / ML", ""
    if re.search(r"\b(security|identity|compliance)\b", lowered):
        return 22, "security / identity", ""
    if re.search(r"\b(full[-\s]?stack|frontend|backend|platform|cloud|infrastructure|distributed)\b", lowered):
        return 23, "general / backend / full-stack", ""
    if SWE_TITLE.search(job.title):
        return 22, "general SWE", ""
    return 12, "adjacent / unknown role", ""


def _location_score(job: Job, prefs: Preferences) -> tuple[int, str]:
    location = normalize_text(job.location)
    if not is_us_location(job.location, is_remote(job)):
        return 0, "non-US location"
    if "new york" in location or "nyc" in location or "manhattan" in location:
        return 20, "NYC"
    if is_remote(job):
        return 18, "Remote USA"
    if any(hub in location for hub in MAJOR_HUBS):
        return (16 if prefs.open_to_relocation else 7), "major hub / relocation"
    return (10 if prefs.open_to_relocation else 4), "other US / relocation"


def _opportunity_score(
    job: Job,
    profile: Profile,
    prefs: Preferences,
    competition: str,
    age: int | None,
) -> tuple[int, list[str]]:
    reasons: list[str] = []
    if age is None:
        points = 2
        reasons.append("age unknown")
    elif age <= 7:
        points = 5
        reasons.append(f"{age}d old")
    elif age <= 14:
        points = 3
        reasons.append(f"{age}d old")
    elif age <= 21:
        points = 2
        reasons.append(f"{age}d old")
    else:
        points = 0
        reasons.append(f"{age}d old")

    boost = COMPETITION_BOOST.get(competition, 0)
    if boost:
        points += boost
        reasons.append(f"{competition} +{boost}")

    text = f"{job.title} {job.company} {(job.description or '')[:1500]}".lower()
    auth = profile.work_auth or {}
    if auth.get("us_citizen") and re.search(r"\b(us citizen|u\.s\. citizen)\b", text):
        points += 3
        reasons.append("US-citizen filter")
    if auth.get("itar_eligible") and "itar" in text:
        points += 3
        reasons.append("ITAR filter")
    if auth.get("clearance_eligible") and "clearance" in text:
        points += 2
        reasons.append("clearance filter")

    points = max(0, min(15, points))
    if any(brand in job.company.lower() for brand in MARQUEE_COMPANIES) and not reasons:
        points = min(points, 5)
    return points, reasons


def _domain_score(job: Job, prefs: Preferences) -> tuple[int, str]:
    text = f"{job.company} {job.title}".lower()
    industries = " ".join(str(item).lower() for item in prefs.industries)
    if any(marker in text for marker in FINANCE_MARKERS):
        return 10 if "fintech" in industries or "finance" in industries else 8, "fintech / enterprise adjacency"
    if any(marker in text for marker in PUBLIC_EDU_MARKERS):
        return 9 if "public" in industries or "education" in industries else 8, "education / public-sector adjacency"
    if any(marker in text for marker in AI_STARTUP_MARKERS):
        return 9 if "ai" in industries or "ml" in industries else 8, "AI / product startup adjacency"
    if any(marker in text for marker in TECH_MARKERS):
        return 7, "tech / security / cloud adjacency"
    return 5, "neutral employer"


def _stack_score(job: Job) -> tuple[int, str]:
    text = f"{job.title} {job.company} {(job.description or '')[:600]}"
    known = set(extract_skills(text))
    if len(known) >= 2:
        return 5, "stack: " + ", ".join(sorted(known)[:4])
    if len(known) == 1:
        return 3, "stack: " + next(iter(known))
    return 1, "stack unknown"


def _confidence(job: Job, competition: str, age: int | None) -> float:
    score = 100.0
    if age is None:
        score -= 20
    if not normalize_text(job.location):
        score -= 20
    if competition == "mass_aggregator":
        score -= 15
    description = job.description or ""
    if len(description.split()) < 120:
        score -= 20
    if not normalize_text(job.company):
        score -= 10
    if not (SWE_TITLE.search(job.title) or NEW_GRAD.search(job.title)):
        score -= 10
    return max(10.0, min(100.0, score))


def _band(score: float, confidence: float) -> str:
    if confidence < 45:
        return "low"
    if score >= 75:
        return "strong"
    if score >= 55:
        return "good"
    if score >= 40:
        return "stretch"
    return "weak"


def quick_scan(
    job: Job,
    *,
    profile: Profile,
    prefs: Preferences,
    archetypes: dict[str, dict[str, Any]],
    scan_cfg: dict[str, Any] | None = None,
) -> ScanResult:
    """Score one job using the resume-forge quick-scan rubric."""
    competition = competition_profile(job)
    age = age_days(job)

    level, level_reason = _level_score(job.title)
    relevance, relevance_reason, archetype = _relevance_score(job, archetypes)
    location, location_reason = _location_score(job, prefs)
    opportunity, opportunity_reasons = _opportunity_score(
        job, profile, prefs, competition, age
    )
    domain, domain_reason = _domain_score(job, prefs)
    stack, stack_reason = _stack_score(job)

    total = level + relevance + location + opportunity + domain + stack
    total = max(0.0, min(100.0, float(total)))

    years = resolve_years(profile, prefs)
    years_required: float | None = None
    years_mentioned = mentions_years(job.description or "")
    if years_mentioned:
        parsed = parse_jd(
            f"{job.title}\n{job.company}\n{job.location}\n{job.description or ''}",
            title_hint=job.title,
            company_hint=job.company,
            location_hint=job.location,
        )
        years_required = parsed.years_required

    years_status = "not_required"
    years_cap: float | None = None
    years_gap = ""
    years_reason = ""
    if years_required is not None:
        if years.value is None:
            years_status = "unknown"
            years_gap = f"Requires {years_required:g}+ years; your years are unknown"
        else:
            delta = years_required - years.value
            have_label = f"{years.value:g}"
            if years.source == "experience":
                have_label += " (estimated)"
            if delta >= 4:
                years_status = "far_short"
                years_gap = (
                    f"Requires {years_required:g}+ years; profile has {have_label} "
                    "— far below the floor"
                )
            elif delta >= 2:
                years_status = "short"
                years_gap = (
                    f"Requires {years_required:g}+ years; profile has {have_label} "
                    "— below the floor"
                )
            elif delta > 0:
                years_status = "close"
                years_gap = (
                    f"Close on experience ({years_required:g}+ required; "
                    f"profile has {have_label})"
                )
            else:
                years_status = "clears"
    elif years_mentioned:
        years_status = "unparsed"
        years_gap = "Years requirement mentioned but not parsed; verify the posting"

    years_cap = YEARS_SCAN_CAPS.get(years_status)
    if years_cap is not None:
        total = min(total, years_cap)
    if (
        years.source == "experience"
        and years.value is not None
        and (years_required is not None or years_mentioned)
    ):
        years_reason = f"experience: {years.value:g} years estimated from dated roles"

    confidence = _confidence(job, competition, age)

    reasons = [
        f"level: {level_reason} ({level})",
        f"role: {relevance_reason} ({relevance})",
        f"location: {location_reason} ({location})",
        f"opportunity: {'; '.join(opportunity_reasons)} ({opportunity})",
        f"domain: {domain_reason} ({domain})",
        f"stack: {stack_reason} ({stack})",
    ]
    if years_reason:
        reasons.append(years_reason)
    gaps: list[str] = []
    if years_gap:
        gaps.append(years_gap)
    if location == 0:
        gaps.append(f"Location outside US/remote: {job.location or 'unknown'}")
    elif location < 10 and not prefs.open_to_relocation:
        gaps.append("Relocation not preferred")
    if age is None:
        gaps.append("Posting age unknown")
    if competition == "mass_aggregator":
        gaps.append("Mass aggregator — higher applicant competition")
    if confidence < 45:
        gaps.append("Low-confidence scan: verify the full posting before deep scoring")

    meta = {
        "components": {
            "level": level,
            "relevance": relevance,
            "location": location,
            "opportunity": opportunity,
            "domain": domain,
            "stack": stack,
        },
        "archetype": archetype,
        "years": {
            "required": years_required,
            "have": years.value,
            "source": years.source,
            "status": years_status,
            "cap": years_cap,
        },
        "source": job.source,
        "age_days": age,
        "lead_only": "community job list" in (job.description or "").lower()
        or "open the employer posting" in (job.description or "").lower(),
    }
    return ScanResult(
        job_id=job.id,
        profile_id=int(profile.id or 0),
        scan_score=round(total, 1),
        confidence=round(confidence, 1),
        band=_band(total, confidence),
        competition_profile=competition,
        age_days=age,
        reasons=reasons,
        gaps=gaps,
        meta=meta,
    )
