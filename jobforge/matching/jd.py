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
YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)(?:\s+of)?", re.IGNORECASE)
YEARS_RANGE = re.compile(
    r"(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
COMPANY_AGE = re.compile(
    r"(?:for|over|more\s+than)\s+\d{1,2}\+?\s+years?,?\s+we(?:'ve|’ve|\s+have)|"
    r"founded\s+\d{1,2}\s+years?\s+ago",
    re.IGNORECASE,
)
DEGREE = re.compile(r"\b(ph\.?d|doctorate|master'?s?|m\.?s\.?|bachelor'?s?|b\.?s\.?|b\.?a\.?|associate'?s?)\b", re.IGNORECASE)
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
    r"demonstrat\w*|hands-on|degree|bachelor\w*|master\w*|phd|minimum|must|requir\w*|"
    r"at\s+least|you\s+have|you\s+are|you[’']ll\s+need|you\s+will\s+need|we\s+require|"
    r"we\s+need|looking\s+for|preferred|plus|design\w*|develop\w*|build\w*|writ\w*|"
    r"implement\w*|maintain\w*|automat\w*|debug\w*|test\w*|deploy\w*|operat\w*|"
    r"collaborat\w*|communicat\w*)\b",
    re.IGNORECASE,
)

BODY_REQUIREMENT_CUE = re.compile(
    r"\b(?:experienc\w*|proficien\w*|knowledg\w*|familiar\w*|abilit\w*|skill\w*|"
    r"background|understand\w*|strong|excellent|solid|comfort\w*|track\s+record|"
    r"demonstrat\w*|hands-on|degree|bachelor\w*|master\w*|phd|minimum|must|requir\w*|"
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
    degree: str | None = None
    clearance: str | None = None
    sponsorship: bool | None = None
    gpa_floor: float | None = None
    citizenship_required: bool = False
    itar_required: bool = False
    salary_min: int | None = None
    salary_max: int | None = None

    @property
    def must_skills(self) -> list[str]:
        return list(dict.fromkeys(r["skill"] for r in self.requirements if r.get("importance") == "must" and r.get("skill")))


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
        r"^(responsibilities|what you'?ll (be doing|do)|what you will (be doing|do)|the role|"
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


def _all_lines(block: str) -> list[str]:
    out: list[str] = []
    for raw in block.splitlines():
        line = BULLET.sub("", raw.strip()).strip()
        if line and not line.endswith(":"):
            out.append(line)
    return out


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
        or DEGREE.search(text)
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


def _degree_level(text: str) -> str | None:
    m = DEGREE.search(text)
    if not m:
        return None
    value = m.group(1).lower()
    if "ph.d" in value or "phd" in value or "doctor" in value:
        return "phd"
    if "master" in value or value in {"m.s", "ms"}:
        return "masters"
    if "bachelor" in value or value in {"b.s", "bs", "b.a", "ba"}:
        return "bachelors"
    if "associate" in value:
        return "associates"
    return None


def parse_jd(
    text: str,
    title_hint: str = "",
    company_hint: str = "",
    location_hint: str = "",
) -> JD:
    text = normalize_text(text or "")
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
        for bullet in _bullets(sections.get("body", "")):
            if _is_requirement_line(bullet, body=True):
                requirement_lines.append((bullet, "must"))

    requirements: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
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
        for skill in skills[:3]:
            key = (skill, importance)
            if key in seen:
                continue
            seen.add(key)
            requirements.append(
                {"text": line[:500], "skill": skill, "importance": importance, "kind": "skill"}
            )

    must_text = "\n".join(line for line, importance in requirement_lines if importance == "must")
    nice_text = "\n".join(line for line, importance in requirement_lines if importance == "nice")
    gate_text = "\n".join(part for part in (must_text, nice_text) if part)

    year_floors: list[float] = []
    for line in must_text.splitlines():
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
        years_required=years_required,
        degree=degree,
        clearance=clearance,
        sponsorship=sponsorship,
        gpa_floor=gpa_floor,
        citizenship_required=citizenship_required,
        itar_required=itar_required,
        salary_min=smin,
        salary_max=smax,
    )
