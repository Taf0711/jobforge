"""Parse a job description into requirements, gates, and skills."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..sources.base import is_remote, normalize_whitespace as normalize_text
from .skills import extract_skills

REQUIREMENT_HEADINGS = {
    "requirements", "qualifications", "minimum qualifications", "basic qualifications",
    "what you'll need", "what you need", "what we're looking for", "who you are",
    "you have", "must have", "must-haves", "required", "skills", "experience",
}
PREFERRED_HEADINGS = {
    "preferred", "preferred qualifications", "nice to have", "nice-to-haves",
    "bonus", "bonus points", "plus", "desired", "desirable",
}
RESPONSIBILITY_HEADINGS = {
    "responsibilities", "what you'll do", "what you will do", "the role", "about the role",
    "duties", "day to day", "day-to-day",
}
OTHER_HEADINGS = {
    "about us", "about the company", "benefits", "perks", "compensation", "salary",
    "equal opportunity", "eeo", "privacy", "how to apply",
}

BULLET = re.compile(r"^\s*(?:[•●▪◦*\-–—]|\d+[.)])\s+")
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'])")
THEMATIC_SECTIONS = {"requirements", "preferred", "responsibilities"}
INLINE_HEADINGS = sorted(
    REQUIREMENT_HEADINGS | PREFERRED_HEADINGS | RESPONSIBILITY_HEADINGS,
    key=len,
    reverse=True,
)
INLINE_HEADING = re.compile(
    r"(^|(?<=[.\n]))[ \t]*(" + "|".join(re.escape(h) for h in INLINE_HEADINGS) + r")[ \t]*:",
    re.IGNORECASE | re.MULTILINE,
)
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:\s+of)?", re.IGNORECASE)
AGE_OF_MAJORITY = re.compile(r"\b\d{1,2}\s*(?:\+|plus)?\s*years?\s+of\s+age\b", re.IGNORECASE)
YEARS_RANGE = re.compile(
    r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
COMPANY_AGE = re.compile(
    r"(?:for|over|more\s+than)\s+\d{1,2}\+?\s+years?,?\s+we(?:'ve|’ve|\s+have)|"
    r"founded\s+\d{1,2}\s+years?\s+ago",
    re.IGNORECASE,
)
# Degree detection is deliberately structural. "Master our development process"
# and "master data management" are prose, not a master's-degree requirement.
PHD_DEGREE = re.compile(r"\b(?:ph\.?\s?d\.?|doctorate|doctoral(?:\s+degree)?)\b", re.IGNORECASE)
BACHELOR_DEGREE = re.compile(
    r"\b(?:bachelor(?:'s|’s|s)?|b\.?\s?sc?\.?|b\.?\s?[sa]\.?)\b",
    re.IGNORECASE,
)
ASSOCIATE_DEGREE = re.compile(
    r"\bassociate(?:'s|’s)?\s+(?:degree|of)\b", re.IGNORECASE
)
MASTER_DEGREE = re.compile(
    r"\bmaster(?:'s|’s|s)\b|\bmaster\s+(?:degree|of|in)\b", re.IGNORECASE
)
MASTER_DOTTED = re.compile(r"\bm\.\s?(?:s|a|sc)\.", re.IGNORECASE)
MASTER_BARE = re.compile(r"\b(?:ms|ma|msc)\b", re.IGNORECASE)
DEGREE_CONTEXT = re.compile(
    r"\b(?:degree|major|field|science|engineering|computer|arts|business)\b",
    re.IGNORECASE,
)
MASTER_ABBREV_CONTEXT = re.compile(r"\b(?:ms|ma|msc)\b\s+(?:in|of)\b", re.IGNORECASE)
DEGREE_PREFERENCE_ONLY = re.compile(
    r"\b(?:preferred|nice\s+to\s+have|a\s+plus|bonus|desired|good\s+to\s+have)\b",
    re.IGNORECASE,
)
DEGREE_REQUIREMENT_MARKER = re.compile(
    r"\b(?:required|require|must|minimum|need|needs|should\s+have|have\s+a|possess|"
    r"or\s+equivalent)\b",
    re.IGNORECASE,
)
CLEARANCE = re.compile(r"\b(ts/sci|top secret|secret clearance|security clearance|public trust|polygraph)\b", re.IGNORECASE)
GPA = re.compile(r"(?:minimum\s+|min\.?\s+)?gpa\s+(?:of\s+)?(\d\.\d+)", re.IGNORECASE)
CITIZENSHIP = re.compile(r"\bu\.?s\.?\s+citizen\b|\bus\s+citizen\b|\bcitizenship\s+required\b", re.IGNORECASE)
ITAR = re.compile(r"\bitar\b", re.IGNORECASE)
NO_SPONSOR = re.compile(r"(?:no|not|unable to|cannot|can't|will not|do not)\s+(?:provide\s+)?(?:visa\s+)?sponsor", re.IGNORECASE)
YES_SPONSOR = re.compile(r"(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:available|provided|offered)", re.IGNORECASE)

# Lines that live inside a requirements section but are not requirements.
BOILERPLATE = re.compile(
    r"^(?:#?li[- ]|req(?:uisition)?\s*(?:id|number)|job\s*(?:id|number)|salary|this\s+salary|"
    r"pay\s*(?:range|rate)|this\s+pay|compensation|base\s*pay|benefits?|additional\s+benefits?|"
    r"perks?|equity|bonus|401|medical|dental|vision|wellness|time\s*off|holiday|hours?|hrs?\b|"
    r"full[- ]time|part[- ]time|multiple\s+positions?|positions?\s+available|telecommut|"
    r"this\s+range|equal\s+opportunity|eeo|privacy|consent|by\s+clicking|how\s+to\s+apply|"
    r"apply\s+now|about\s+(?:us|the\s+company)|who\s+we\s+are|who\s+are\s+we|"
    r"our\s+(?:story|mission|culture)|why\s+(?:join|work)|what\s+we\s+offer|we\s+offer|"
    r"we\s+provide|we\s+are|we[’']re|we[’']ve|we\s+have|you[’']ll\s+be|you\s+will\s+be|"
    r"in\s+our|in\s+this\s+role|this\s+role|the\s+team|the\s+role|for\s+positions|"
    r"health\s+coverage|spending\s+stipends|own\s+a\s+piece)\b",
    re.IGNORECASE,
)

REQUIREMENT_CUE = re.compile(
    r"\b(?:experienc\w*|proficien\w*|knowledg\w*|familiar\w*|abilit\w*|skill\w*|"
    r"background|understand\w*|strong|excellent|solid|comfort\w*|track\s+record|"
    r"demonstrat\w*|hands-on|degree|bachelor\w*|phd|minimum|must|requir\w*|"
    r"at\s+least|you\s+have|you\s+are|you[’']ll\s+need|you\s+will\s+need|we\s+require|"
    r"we\s+need|looking\s+for|preferred|plus|design\w*|develop\w*|build\w*|writ\w*|"
    r"implement\w*|maintain\w*|automat\w*|debug\w*|test\w*|deploy\w*|operat\w*|"
    r"collaborat\w*|communicat\w*)\b",
    re.IGNORECASE,
)

BODY_REQUIREMENT_CUE = re.compile(
    r"\b(?:experienc\w*|proficien\w*|knowledg\w*|familiar\w*|abilit\w*|skill\w*|"
    r"background|understand\w*|strong|excellent|solid|comfort\w*|track\s+record|"
    r"demonstrat\w*|hands-on|degree|bachelor\w*|phd|minimum|must|requir\w*|"
    r"at\s+least|you\s+have|you\s+are|you[’']ll\s+need|you\s+will\s+need)\b",
    re.IGNORECASE,
)


@dataclass
class JD:
    title: str = ""
    company: str = ""
    location: str = ""
    remote: bool = False
    description: str = ""
    requirements: list[dict[str, Any]] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    years_required: float | None = None
    years_mentioned: bool = False
    degree: str | None = None
    sections: dict[str, str] = field(default_factory=dict)
    clearance: str | None = None
    sponsorship: bool | None = None
    gpa_floor: float | None = None
    citizenship_required: bool = False
    itar_required: bool = False
    salary_min: int | None = None
    salary_max: int | None = None

    @property
    def must_skills(self) -> list[str]:
        return list(
            dict.fromkeys(
                skill
                for row in self.requirements
                if row.get("importance") == "must"
                for skill in (row.get("skills") or ([row["skill"]] if row.get("skill") else []))
            )
        )


def _heading(line: str) -> str | None:
    key = line.lower().replace("’", "'").replace("‘", "'")
    key = re.sub(r"^[#*_\s]+|[#*_\s]+$", "", key)
    key = re.sub(r"[^a-z0-9 /&'+-]", "", key).strip()
    key = re.sub(r"\s+", " ", key).rstrip(":")
    if len(key) > 60:
        return None
    if re.match(
        r"^(what you should have|what we(?:'re| are) looking for|what we look for|what we require|"
        r"what we need|what you'?ll (?:need|bring)|what you bring|who you are|about you|you have|"
        r"you'?ll have|you will have|the ideal candidate(?: has| will have)?|minimum (?:qualifications|requirements)|"
        r"basic (?:qualifications|requirements)|required qualifications|skills (?:and|&) experience|"
        r"what it takes|you might be a good fit if|you'?d be a great fit if|requirements|"
        r"requirements and qualifications|qualifications|must have|must-haves|skills|experience)$",
        key,
    ):
        return "requirements"
    if re.match(
        r"^(preferred|preferred qualifications|nice to have|nice-to-haves|good to have|it'?s a plus|"
        r"bonus|bonus points|plus|desired|desirable)$",
        key,
    ):
        return "preferred"
    if re.match(
        r"^(responsibilities|key (?:job )?responsibilities|primary responsibilities|"
        r"what you'?ll (be doing|do)|what you will (be doing|do)|the role|"
        r"about the role|in this role|your impact|duties|day to day|day-to-day)$",
        key,
    ):
        return "responsibilities"
    if re.match(
        r"^(about|about us|about the company|who we are|who are we|our story|mission|culture|life at .*|"
        r"why .*|benefits|perks|compensation|salary|base salary|pay range|salary range|"
        r"equal opportunity|eeo|privacy|accommodations|how to apply|apply|what we offer)$",
        key,
    ):
        return "other"
    return None


def split_jd_sections(text: str) -> dict[str, str]:
    sections: dict[str, list[str]] = {"body": []}
    current = "body"
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        h = _heading(line)
        if h:
            current = h
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def promote_inline_headings(text: str) -> str:
    """Put headings that were flattened into a paragraph back on their own line.

    Many sources deliver a posting as one line ("... impact. BASIC QUALIFICATIONS:
    Experience with ..."), which hides every section from :func:`split_jd_sections`.
    Only headings that a sentence boundary or line start introduces are promoted.
    """
    if not text:
        return text

    def repl(match: re.Match[str]) -> str:
        return f"{match.group(1)}\n{match.group(2)}:\n"

    return INLINE_HEADING.sub(repl, text)


def _bullets(block: str) -> list[str]:
    out: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        if BULLET.match(line):
            out.append(BULLET.sub("", line).strip())
        elif len(line) > 40 and not line.endswith(":"):
            out.append(line)
    return out


def _strict_bullets(block: str) -> list[str]:
    """Only actual bullets; body paragraphs are not requirements."""
    out: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if BULLET.match(line):
            text = BULLET.sub("", line).strip()
            if text:
                out.append(text)
    return out


def _all_lines(block: str) -> list[str]:
    out: list[str] = []
    for raw in block.splitlines():
        line = BULLET.sub("", raw.strip()).strip()
        if not line or line.endswith(":"):
            continue
        if BULLET.match(raw) or len(line) < 40:
            out.append(line)
            continue
        # Flattened postings keep a whole requirements block on one line.
        # One clause is one row, so split the paragraph back into sentences.
        parts = [part.strip() for part in SENTENCE_SPLIT.split(line) if part.strip()]
        out.extend(parts if len(parts) > 1 else [line])
    return out


def mentions_years(text: str) -> bool:
    """Return True when the posting mentions a numeric year requirement.

    This is deliberately broader than ``years_required``: a line outside a
    recognized requirements section still means the human should verify the
    floor rather than treating the posting as having no year requirement.
    Company-age and boilerplate lines are ignored.
    """
    for raw in (text or "").splitlines():
        line = BULLET.sub("", raw.strip()).strip()
        if not line or COMPANY_AGE.search(line) or AGE_OF_MAJORITY.search(line):
            continue
        if BOILERPLATE.match(line):
            continue
        if YEARS.search(line):
            return True
    return False


def _is_requirement_line(line: str, *, body: bool = False) -> bool:
    text = BULLET.sub("", (line or "").strip()).strip()
    if not text or text.endswith(":"):
        return False
    if BOILERPLATE.match(text) or COMPANY_AGE.search(text):
        return False
    if re.match(r"^(we|our)\b", text, re.IGNORECASE) and not re.match(
        r"^(we\s+(require|need|are\s+looking)|our\s+(requirements|qualifications))",
        text,
        re.IGNORECASE,
    ):
        return False
    if (
        YEARS.search(text)
        or _degree_levels(text)
        or CLEARANCE.search(text)
        or GPA.search(text)
        or CITIZENSHIP.search(text)
        or ITAR.search(text)
        or NO_SPONSOR.search(text)
        or YES_SPONSOR.search(text)
    ):
        return True
    if extract_skills(text):
        return True
    cue = BODY_REQUIREMENT_CUE if body else REQUIREMENT_CUE
    return bool(cue.search(text))


def _has_degree_context(text: str) -> bool:
    """True when a bare MS/MA/MSc abbreviation is used as a degree."""
    return (
        bool(DEGREE_CONTEXT.search(text))
        or bool(MASTER_ABBREV_CONTEXT.search(text))
        or any(
            pattern.search(text)
            for pattern in (PHD_DEGREE, BACHELOR_DEGREE, ASSOCIATE_DEGREE, MASTER_DOTTED)
        )
    )


def _degree_levels(text: str) -> set[str]:
    """Return every degree level mentioned in one clause or line."""
    levels: set[str] = set()
    if PHD_DEGREE.search(text):
        levels.add("phd")
    if BACHELOR_DEGREE.search(text):
        levels.add("bachelors")
    if ASSOCIATE_DEGREE.search(text):
        levels.add("associates")
    if MASTER_DEGREE.search(text) or MASTER_DOTTED.search(text):
        levels.add("masters")
    elif MASTER_BARE.search(text) and _has_degree_context(text):
        levels.add("masters")
    return levels


def _degree_level(text: str) -> str | None:
    """Return the lowest degree floor, ignoring preferred-only clauses.

    A posting may say "Bachelor's required; Master's preferred", which is a
    bachelor's floor. It may also say only "Master's preferred", which is not
    a floor at all. Splitting clauses before applying the preference filter
    keeps both cases correct.
    """
    levels: set[str] = set()
    for raw_line in (text or "").splitlines():
        for clause in re.split(r";\s*", raw_line):
            clause = clause.strip()
            if not clause:
                continue
            if (
                DEGREE_PREFERENCE_ONLY.search(clause)
                and not DEGREE_REQUIREMENT_MARKER.search(clause)
            ):
                continue
            levels |= _degree_levels(clause)
    for level in ("associates", "bachelors", "masters", "phd"):
        if level in levels:
            return level
    return None


def parse_jd(
    text: str,
    title_hint: str = "",
    company_hint: str = "",
    location_hint: str = "",
) -> JD:
    text = normalize_text(text or "")
    years_mentioned = mentions_years(text)
    sections = split_jd_sections(text)
    if not THEMATIC_SECTIONS & sections.keys():
        promoted = promote_inline_headings(text)
        if promoted != text:
            text = promoted
            sections = split_jd_sections(text)

    requirement_lines: list[tuple[str, str]] = []
    for heading, section in sections.items():
        if heading in {"requirements", "preferred", "responsibilities"}:
            importance = "nice" if heading == "preferred" else ("must" if heading == "requirements" else "context")
            lines = _all_lines(section) if heading in {"requirements", "preferred"} else _bullets(section)
            for bullet in lines:
                if _is_requirement_line(bullet, body=False):
                    requirement_lines.append((bullet, importance))

    if not any(importance == "must" for _, importance in requirement_lines):
        # A paragraph in the body is not a requirement just because it mentions
        # a skill. Require an actual bullet; recognized requirements sections
        # above may still use paragraph lines.
        for bullet in _strict_bullets(sections.get("body", "")):
            if _is_requirement_line(bullet, body=True):
                requirement_lines.append((bullet, "must"))

    requirements: list[dict[str, Any]] = []
    seen_lines: set[tuple[str, str]] = set()
    for line, importance in requirement_lines:
        line_key = (" ".join(line.lower().split()), importance)
        if line_key in seen_lines:
            continue
        seen_lines.add(line_key)
        skills = extract_skills(line)
        if not skills:
            requirements.append(
                {"text": line[:500], "skill": None, "importance": importance, "kind": "prose"}
            )
            continue
        # One clause is one row. "Java, Rust, Scala, C++, or C" is an OR-list;
        # emitting one full-weight row per alternative both double-counts and
        # falsely marks unmet alternatives as gaps.
        requirements.append(
            {
                "text": line[:500],
                "skill": skills[0],
                "skills": skills,
                "importance": importance,
                "kind": "skill",
            }
        )

    must_text = "\n".join(line for line, importance in requirement_lines if importance == "must")
    nice_text = "\n".join(line for line, importance in requirement_lines if importance == "nice")
    gate_text = "\n".join(part for part in (must_text, nice_text) if part)

    year_floors: list[float] = []
    for line in must_text.splitlines():
        if AGE_OF_MAJORITY.search(line):
            continue
        ranges = YEARS_RANGE.findall(line)
        if ranges:
            year_floors.extend(min(float(left), float(right)) for left, right in ranges)
            continue
        year_floors.extend(float(m.group(1)) for m in YEARS.finditer(line))
    years_required = max(year_floors) if year_floors else None
    degree = _degree_level(must_text)
    clearance_match = CLEARANCE.search(gate_text)
    clearance = clearance_match.group(1).lower() if clearance_match else None
    gpa_match = GPA.search(must_text)
    gpa_floor = float(gpa_match.group(1)) if gpa_match else None
    citizenship_required = bool(CITIZENSHIP.search(gate_text))
    itar_required = bool(ITAR.search(gate_text))
    if NO_SPONSOR.search(text):
        sponsorship = False
    elif YES_SPONSOR.search(text):
        sponsorship = True
    else:
        sponsorship = None

    location = location_hint.strip()
    if not location:
        loc_match = re.search(r"(?:location|based in|office)\s*[:\-]\s*([^\n]{2,100})", text, re.IGNORECASE)
        if loc_match:
            location = loc_match.group(1).strip()
    remote = is_remote(location, text)

    from ..sources.base import parse_salary

    smin, smax = parse_salary(text)
    all_skills = extract_skills(text)

    return JD(
        title=title_hint.strip(),
        company=company_hint.strip(),
        location=location,
        remote=remote,
        description=text,
        requirements=requirements,
        skills=all_skills,
        sections=dict(sections),
        years_required=years_required,
        years_mentioned=years_mentioned,
        degree=degree,
        clearance=clearance,
        sponsorship=sponsorship,
        gpa_floor=gpa_floor,
        citizenship_required=citizenship_required,
        itar_required=itar_required,
        salary_min=smin,
        salary_max=smax,
    )
