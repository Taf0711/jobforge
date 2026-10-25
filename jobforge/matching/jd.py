"""Parse a job description into requirements, gates, and skills."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..sources.base import normalize_whitespace as normalize_text
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
DEGREE = re.compile(r"\b(ph\.?d|doctorate|master'?s?|m\.?s\.?|bachelor'?s?|b\.?s\.?|b\.?a\.?|associate'?s?)\b", re.IGNORECASE)
CLEARANCE = re.compile(r"\b(ts/sci|top secret|secret clearance|security clearance|public trust|polygraph)\b", re.IGNORECASE)
GPA = re.compile(r"(?:minimum\s+|min\.?\s+)?gpa\s+(?:of\s+)?(\d\.\d+)", re.IGNORECASE)
CITIZENSHIP = re.compile(r"\bu\.?s\.?\s+citizen\b|\bus\s+citizen\b|\bcitizenship\s+required\b", re.IGNORECASE)
ITAR = re.compile(r"\bitar\b", re.IGNORECASE)
NO_SPONSOR = re.compile(r"(?:no|not|unable to|cannot|can't|will not|do not)\s+(?:provide\s+)?(?:visa\s+)?sponsor", re.IGNORECASE)
YES_SPONSOR = re.compile(r"(?:visa\s+)?sponsorship\s+(?:is\s+)?(?:available|provided|offered)", re.IGNORECASE)


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
    key = re.sub(r"[^a-z0-9 /&'-]", "", line.lower()).strip()
    key = re.sub(r"\s+", " ", key).rstrip(":")
    if len(key) > 60:
        return None
    if re.match(
        r"^(what you should have|what we'?re looking for|what we require|what we need|what you'?ll need|"
        r"who you are|you have|requirements|requirements and qualifications|qualifications|"
        r"minimum qualifications|basic qualifications|must have|must-haves|skills|experience)$",
        key,
    ):
        return "requirements"
    if re.match(
        r"^(preferred|preferred qualifications|nice to have|nice-to-haves|bonus|bonus points|plus|desired|desirable)$",
        key,
    ):
        return "preferred"
    if re.match(
        r"^(responsibilities|what you'?ll (be doing|do)|what you will (be doing|do)|the role|"
        r"about the role|duties|day to day|day-to-day)$",
        key,
    ):
        return "responsibilities"
    if re.match(
        r"^(about|about us|about the company|why .*|benefits|perks|compensation|salary|base salary|"
        r"pay range|salary range|equal opportunity|eeo|privacy|accommodations|how to apply|apply)$",
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


def _degree_level(text: str) -> str | None:
    m = DEGREE.search(text)
    if not m:
        return None
    value = m.group(1).lower()
    if "ph" in value or "doctor" in value:
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
                requirement_lines.append((bullet, importance))

    if not any(importance == "must" for _, importance in requirement_lines):
        for bullet in _bullets(sections.get("body", "")):
            if re.match(r"^(we |our |at |join |welcome|about |why )", bullet, re.IGNORECASE):
                continue
            if extract_skills(bullet) or YEARS.search(bullet) or re.search(
                r"\b(experience|proficien|knowledge|familiar|degree|ability|skills?|background|understanding)\b",
                bullet,
                re.IGNORECASE,
            ):
                requirement_lines.append((bullet, "must"))

    requirements: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for line, importance in requirement_lines:
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

    years = [float(m.group(1)) for m in YEARS.finditer(text)]
    years_required = max(years) if years else None
    degree = _degree_level(text)
    clearance_match = CLEARANCE.search(text)
    clearance = clearance_match.group(1).lower() if clearance_match else None
    gpa_match = GPA.search(text)
    gpa_floor = float(gpa_match.group(1)) if gpa_match else None
    citizenship_required = bool(CITIZENSHIP.search(text))
    itar_required = bool(ITAR.search(text))
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
    remote = bool(re.search(r"\b(remote|work from home|distributed)\b", f"{location} {text[:3000]}", re.IGNORECASE))

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
