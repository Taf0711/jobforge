"""Resume text extraction and heuristic parsing.

This is deliberately dependency-light: PyMuPDF for PDFs, zipfile for DOCX,
plain text for TXT/MD. The parser is conservative — it keeps raw bullets so a
human can correct the structured profile in YAML.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

import pymupdf
import yaml

from ..models import Profile, slugify
from .skills import canonical, extract_skills

SECTION_ALIASES = {
    "summary": ["summary", "profile", "professional summary", "objective", "about"],
    "experience": ["experience", "work experience", "employment", "professional experience", "employment history"],
    "education": ["education", "academic background", "academics"],
    "projects": ["projects", "selected projects", "technical projects", "personal projects"],
    "skills": ["skills", "technical skills", "core skills", "technologies", "toolkit"],
    "certifications": ["certifications", "certificates", "licenses", "awards", "honors"],
}

HEADING_TO_SECTION = {
    alias.lower(): section for section, aliases in SECTION_ALIASES.items() for alias in aliases
}

DATE_RANGE = re.compile(
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*\d{4}|\d{4}|\d{1,2}/\d{4})"
    r"\s*(?:-|–|—|to)\s*"
    r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s*\d{4}|\d{4}|present|current|\d{1,2}/\d{4})",
    re.IGNORECASE,
)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")
URL = re.compile(r"https?://[^\s)>\]]+")
LINKEDIN = re.compile(r"(?:linkedin\.com/in/|linkedin\.com/pub/)[^\s,;]+", re.IGNORECASE)
GITHUB = re.compile(r"(?:github\.com/)[^\s,;]+", re.IGNORECASE)
BULLET = re.compile(r"^\s*(?:[•●▪◦*\-–—]|\d+[.)])\s+")


def extract_text(path: str | Path) -> str:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        doc = pymupdf.open(p)
        return "\n".join(page.get_text("text") for page in doc).strip()
    if suffix == ".docx":
        with zipfile.ZipFile(p) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
        xml = xml.replace("</w:p>", "\n").replace("<w:br/>", "\n")
        text = re.sub(r"<[^>]+>", "", xml)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    if suffix in {".txt", ".md", ".markdown"}:
        return p.read_text(encoding="utf-8", errors="replace").strip()
    raise ValueError(f"Unsupported resume format: {suffix} (use PDF, DOCX, TXT, or MD)")


def _clean_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.replace("\xa0", " ").strip()
        line = re.sub(r"[ \t]+", " ", line)
        if line:
            lines.append(line)
    return lines


def split_sections(text: str) -> dict[str, str]:
    lines = _clean_lines(text)
    sections: dict[str, list[str]] = {}
    current = "header"
    for line in lines:
        key = re.sub(r"[^a-z ]", "", line.lower()).strip()
        key = re.sub(r"\s+", " ", key)
        if key in HEADING_TO_SECTION and len(line) <= 48:
            current = HEADING_TO_SECTION[key]
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _parse_name(header_lines: list[str], fallback: str) -> tuple[str, str]:
    for line in header_lines[:4]:
        if EMAIL.search(line) or PHONE.search(line) or len(line) > 50:
            continue
        words = line.split()
        if 1 < len(words) <= 5 and all(re.search(r"[A-Za-z]", w) for w in words):
            if any(token in line.lower() for token in ("resume", "curriculum", "vitae")):
                continue
            return words[0], " ".join(words[1:])
    parts = fallback.split()
    return (parts[0] if parts else ""), (" ".join(parts[1:]) if len(parts) > 1 else "")


def _parse_links(text: str) -> dict[str, str]:
    links: dict[str, str] = {}
    if m := LINKEDIN.search(text):
        links["linkedin"] = "https://" + m.group(0)
    if m := GITHUB.search(text):
        links["github"] = "https://" + m.group(0)
    for url in URL.findall(text):
        if "linkedin.com" in url or "github.com" in url:
            continue
        links.setdefault("portfolio", url)
        break
    return links


def _empty_entry() -> dict[str, Any]:
    return {"title": "", "company": "", "location": "", "start": "", "end": "", "bullets": []}


def _is_next_entry(lines: list[str], i: int) -> bool:
    """A non-bullet line immediately before a date range is a company header."""
    return i + 1 < len(lines) and bool(DATE_RANGE.search(lines[i + 1]))


def _parse_experience(block: str) -> list[dict[str, Any]]:
    if not block.strip():
        return []
    lines = _clean_lines(block)
    entries: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if BULLET.match(line):
            if not entries:
                entries.append(_empty_entry())
            text = BULLET.sub("", line).strip()
            if text:
                entries[-1]["bullets"].append(text)
            i += 1
            # Absorb wrapped continuation lines (lowercase prose without a new header).
            while i < len(lines) and not BULLET.match(lines[i]) and not DATE_RANGE.search(lines[i]):
                nxt = lines[i].strip()
                if _is_next_entry(lines, i):
                    break
                if entries[-1]["bullets"] and not entries[-1]["bullets"][-1].rstrip().endswith((".", "!", "?")):
                    entries[-1]["bullets"][-1] = (entries[-1]["bullets"][-1] + " " + nxt).strip()
                    i += 1
                else:
                    break
            continue

        dates = DATE_RANGE.search(line)
        if " | " in line or " — " in line:
            start, end = (dates.group(1), dates.group(2)) if dates else ("", "")
            header = DATE_RANGE.sub("", line).strip(" |—-–")
            parts = [p.strip() for p in re.split(r"\s*[|—–]\s*", header) if p.strip()]
            entries.append(
                {
                    "title": parts[0] if parts else header,
                    "company": parts[1] if len(parts) > 1 else "",
                    "location": parts[2] if len(parts) > 2 else "",
                    "start": start,
                    "end": end,
                    "bullets": [],
                }
            )
            i += 1
            continue

        if dates:
            # Layout: Company / Date range / Title / Location
            company = lines[i - 1].strip() if i > 0 and not BULLET.match(lines[i - 1]) else ""
            title = lines[i + 1].strip() if i + 1 < len(lines) and not BULLET.match(lines[i + 1]) else ""
            location = ""
            if i + 2 < len(lines) and not BULLET.match(lines[i + 2]) and not DATE_RANGE.search(lines[i + 2]):
                location = lines[i + 2].strip()
            entries.append(
                {
                    "title": title,
                    "company": company,
                    "location": location,
                    "start": dates.group(1),
                    "end": dates.group(2),
                    "bullets": [],
                }
            )
            i += 1
            if title:
                i += 1
            if location:
                i += 1
            continue

        i += 1
    return entries


def _parse_education(block: str) -> list[dict[str, Any]]:
    lines = _clean_lines(block)
    entries: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line or BULLET.match(line):
            i += 1
            continue
        dates = DATE_RANGE.search(line)
        if dates:
            institution = lines[i - 1].strip() if i > 0 and not BULLET.match(lines[i - 1]) else ""
            degree = lines[i + 1].strip() if i + 1 < len(lines) and not BULLET.match(lines[i + 1]) else ""
            location = ""
            if i + 2 < len(lines) and not BULLET.match(lines[i + 2]) and not DATE_RANGE.search(lines[i + 2]):
                location = lines[i + 2].strip()
            degree_value, major_value = degree, location
            if " in " in degree and not major_value:
                degree_value, major_value = [p.strip() for p in degree.split(" in ", 1)]
            elif " in " in degree and major_value:
                degree_value, extra_major = [p.strip() for p in degree.split(" in ", 1)]
                major_value = extra_major or major_value
            entries.append(
                {
                    "institution": institution,
                    "degree": degree_value,
                    "major": major_value,
                    "start": dates.group(1),
                    "end": dates.group(2),
                }
            )
            i += 1
            if degree:
                i += 1
            if location:
                i += 1
            continue
        if " | " in line:
            parts = [p.strip() for p in re.split(r"\s*\|\s*", line) if p.strip()]
            entries.append(
                {
                    "institution": parts[0] if parts else line,
                    "degree": parts[1] if len(parts) > 1 else "",
                    "major": parts[2] if len(parts) > 2 else "",
                    "start": "",
                    "end": "",
                }
            )
        i += 1
    return entries


def _parse_projects(block: str) -> list[dict[str, Any]]:
    return _parse_experience(block)


def _parse_skills(block: str, full_text: str) -> list[str]:
    candidates: list[str] = []
    category_labels = {"languages", "technologies", "concepts", "developer tools", "tools", "frameworks"}
    for line in _clean_lines(block):
        line = BULLET.sub("", line).strip()
        if ":" in line:
            prefix, line = line.split(":", 1)
            if prefix.strip().lower() not in category_labels and len(prefix) > 24:
                line = prefix + " " + line
        for token in re.split(r"[,|;/•●▪◦]", line):
            token = re.sub(r"\([^)]*\)", "", token).strip(" .:-")
            if 1 < len(token) <= 40 and not token.lower().startswith(("and ", "or ")):
                candidates.append(token)
    out: list[str] = []
    for token in candidates:
        canon = canonical(token)
        if canon.lower() in category_labels:
            continue
        if canon.lower() in {c.lower() for c in out}:
            continue
        if canon in extract_skills(token) or (2 <= len(token.split()) <= 4 and not token.endswith(".")):
            out.append(canon)
    if not out:
        out = extract_skills(full_text)[:30]
    return out


def parse_resume(text: str, fallback_name: str = "default") -> Profile:
    sections = split_sections(text)
    header = sections.get("header", "").splitlines()
    first, last = _parse_name(header, fallback_name)
    email = EMAIL.search(text)
    phone = PHONE.search(text)
    links = _parse_links(text)
    experience = _parse_experience(sections.get("experience", ""))
    education = _parse_education(sections.get("education", ""))
    projects = _parse_projects(sections.get("projects", ""))
    certifications = _parse_education(sections.get("certifications", ""))
    skills = _parse_skills(sections.get("skills", ""), text)
    summary = sections.get("summary", "").strip()

    facts: list[dict[str, Any]] = []
    for entry in [*experience, *projects]:
        for bullet in entry.get("bullets", []):
            fact_id = f"fact-{len(facts) + 1:03d}"
            facts.append(
                {
                    "id": fact_id,
                    "section": "experience" if entry in experience else "projects",
                    "title": entry.get("title", ""),
                    "company": entry.get("company", ""),
                    "text": bullet,
                    "skills": extract_skills(bullet),
                }
            )

    location = next((e.get("location", "") for e in experience if e.get("location")), "")
    return Profile(
        name=slugify(fallback_name or "default"),
        resume_text=text,
        location=location,
        first_name=first,
        last_name=last,
        email=email.group(0) if email else "",
        phone=phone.group(0).strip() if phone else "",
        links=links,
        headline=header[1] if len(header) > 1 and len(header[1]) < 100 else "",
        summary=summary,
        experience=experience,
        education=education,
        projects=projects,
        certifications=certifications,
        skills=skills,
        facts=facts,
    )


def profile_from_resume(path: str | Path, name: str | None = None) -> Profile:
    p = Path(path)
    text = extract_text(p)
    profile = parse_resume(text, fallback_name=name or p.stem)
    profile.resume_path = str(p)
    return profile


def load_profile_file(path: str | Path) -> Profile:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) if p.suffix.lower() in {".yml", ".yaml"} else json.loads(p.read_text(encoding="utf-8"))
    return Profile.from_dict(data or {})


def merge_profiles(base: Profile, override: Profile) -> Profile:
    merged = Profile.from_dict(base.to_dict())
    for field_name in Profile.__dataclass_fields__:
        value = getattr(override, field_name)
        if value not in (None, "", [], {}):
            setattr(merged, field_name, value)
    return merged
