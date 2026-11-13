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
COURSES = re.compile(
    r"^\s*(?:[•●▪◦*\-–—]|\d+[.)])?\s*(?:relevant\s+)?(?:coursework|courses)\s*[:\-–—]\s*(.*)$",
    re.IGNORECASE,
)


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


def _course_values(value: str) -> list[str]:
    courses = []
    for item in re.split(r"[,;]", value):
        item = re.sub(r"\s+", " ", item).strip(" .;")
        if item:
            courses.append(item)
    return courses


def _parse_education(block: str) -> list[dict[str, Any]]:
    lines = _clean_lines(block)
    entries: list[dict[str, Any]] = []
    pending_courses: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        # A course list often wraps across lines in the PDF text layer
        # ("Database\nManagement, Software Engineering"). Collect the marker
        # line plus its continuation before the normal entry parser skips it.
        marker = COURSES.match(line)
        if marker:
            value = marker.group(1).strip()
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if BULLET.match(nxt) or DATE_RANGE.search(nxt) or COURSES.match(nxt):
                    break
                if nxt.rstrip().endswith("."):
                    break
                # A new institution is usually followed by its date line.
                if (
                    j + 1 < len(lines)
                    and DATE_RANGE.search(lines[j + 1])
                    and not nxt.rstrip().endswith(",")
                ):
                    break
                value += " " + nxt.strip()
                j += 1
            courses = _course_values(value)
            if entries:
                entries[-1].setdefault("courses", []).extend(courses)
            else:
                pending_courses.extend(courses)
            i = j
            continue
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
            entry = {
                "institution": institution,
                "degree": degree_value,
                "major": major_value,
                "start": dates.group(1),
                "end": dates.group(2),
            }
            if pending_courses:
                entry["courses"] = pending_courses
                pending_courses = []
            entries.append(entry)
            i += 1
            if degree:
                i += 1
            if location:
                i += 1
            continue
        if " | " in line:
            parts = [p.strip() for p in re.split(r"\s*\|\s*", line) if p.strip()]
            entry = {
                "institution": parts[0] if parts else line,
                "degree": parts[1] if len(parts) > 1 else "",
                "major": parts[2] if len(parts) > 2 else "",
                "start": "",
                "end": "",
            }
            if pending_courses:
                entry["courses"] = pending_courses
                pending_courses = []
            entries.append(entry)
        i += 1
    if pending_courses and entries:
        entries[-1].setdefault("courses", []).extend(pending_courses)
    return entries


PROJECT_LINK_LABELS = {
    "code",
    "demo",
    "github",
    "link",
    "live",
    "paper",
    "repo",
    "repository",
    "slides",
    "video",
    "website",
}


def _project_url(text: str) -> str:
    match = URL.search(text)
    if match:
        return match.group(0).rstrip(".,;)")
    match = re.search(r"(?:www\.)?github\.com/[^\s,;|]+", text, re.IGNORECASE)
    return ("https://" + match.group(0)) if match else ""


def _looks_like_tech_line(line: str) -> bool:
    lowered = line.strip().lower()
    if lowered.startswith(("tech:", "technologies:", "stack:", "tools:")):
        return True
    if len(line) > 180:
        return False
    skills = extract_skills(line)
    if len(skills) >= 2:
        return True
    if "," not in line:
        return False
    tokens = [token.strip() for token in line.split(",") if token.strip()]
    if len(tokens) < 2:
        return False
    known = sum(
        1
        for token in tokens
        if canonical(token)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9.+#/_-]{1,24}", token)
    )
    return known >= 2


def _looks_like_project_title(line: str) -> bool:
    text = line.strip()
    if not text or len(text) > 120 or text.endswith((".", "!", "?")):
        return False
    lowered = text.lower()
    if lowered.startswith(
        (
            "a ",
            "an ",
            "and ",
            "as ",
            "at ",
            "built ",
            "by ",
            "created ",
            "designed ",
            "developed ",
            "engineered ",
            "for ",
            "from ",
            "implemented ",
            "in ",
            "it ",
            "led ",
            "managed ",
            "migrated ",
            "on ",
            "owned ",
            "reduced ",
            "scaled ",
            "shipped ",
            "the ",
            "then ",
            "this ",
            "to ",
            "we ",
            "with ",
            "worked ",
            "wrote ",
        )
    ):
        return False
    if text[:1].isupper():
        return True
    return bool(re.match(r"^[a-z][\w.-]*\s+\([^)]+\)$", text))


def _split_project_header(
    line: str,
) -> tuple[str, str, str, str, str]:
    dates = DATE_RANGE.search(line)
    start, end = (dates.group(1), dates.group(2)) if dates else ("", "")
    header = DATE_RANGE.sub("", line).strip(" |—-–")
    url = _project_url(header)
    if url:
        header = header.replace(url, " ").strip(" |—-–")
    parts = [part.strip() for part in re.split(r"\s*[|—–]\s*", header) if part.strip()]
    title = parts[0] if parts else header
    tech_parts: list[str] = []
    for part in parts[1:]:
        if part.lower() in PROJECT_LINK_LABELS:
            continue
        if _project_url(part):
            continue
        tech_parts.append(part)
    return title, ", ".join(tech_parts), start, end, url


def _parse_projects(block: str) -> list[dict[str, Any]]:
    """Parse a projects section without treating a tech line as a company.

    Projects have a different shape from jobs: ``Title | Tech line`` followed
    by bullets, often with no dates or company. Reusing the experience parser
    merged every project into one entry and swallowed the next title into the
    previous bullet when the bullet did not end with punctuation.
    """
    if not block.strip():
        return []
    lines = _clean_lines(block)
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    previous_was_bullet = False

    def start_entry(line: str) -> dict[str, Any]:
        title, tech_line, start, end, url = _split_project_header(line)
        return {
            "title": title,
            "company": "",
            "location": "",
            "start": start,
            "end": end,
            "tech_line": tech_line,
            "url": url,
            "bullets": [],
        }

    for index, line in enumerate(lines):
        if BULLET.match(line):
            if current is None:
                current = {
                    "title": "",
                    "company": "",
                    "location": "",
                    "start": "",
                    "end": "",
                    "tech_line": "",
                    "url": "",
                    "bullets": [],
                }
                entries.append(current)
            text = BULLET.sub("", line).strip()
            if text:
                current["bullets"].append(text)
            previous_was_bullet = True
            continue

        # Date-only and URL-only lines belong to the project above them.
        date_only = DATE_RANGE.fullmatch(line.strip())
        if current is not None and date_only:
            current["start"] = date_only.group(1)
            current["end"] = date_only.group(2)
            previous_was_bullet = False
            continue

        line_url = _project_url(line)
        remainder = re.sub(
            r"https?://\S+|(?:www\.)?github\.com/\S+", "", line
        ).strip(" |—-–").lower()
        url_only = bool(line_url) and (
            not remainder or remainder in PROJECT_LINK_LABELS
        )
        if current is not None and url_only:
            current["url"] = line_url
            previous_was_bullet = False
            continue

        if current is not None and not current["bullets"] and _looks_like_tech_line(line):
            tech_line = re.sub(
                r"^(?:tech|technologies|stack|tools)\s*:\s*", "", line, flags=re.IGNORECASE
            ).strip()
            current["tech_line"] = (
                f"{current['tech_line']}, {tech_line}".strip(", ")
                if current["tech_line"]
                else tech_line
            )
            previous_was_bullet = False
            continue

        has_header_separator = bool(re.search(r"\s[|—–]\s", line))
        dates = DATE_RANGE.search(line)
        is_link_label = line.strip().lower() in PROJECT_LINK_LABELS
        if is_link_label:
            previous_was_bullet = False
            continue

        next_is_bullet = index + 1 < len(lines) and bool(BULLET.match(lines[index + 1]))
        previous_bullet_closed = (
            previous_was_bullet
            and current is not None
            and bool(current["bullets"])
            and current["bullets"][-1].rstrip().endswith((".", "!", "?"))
        )
        starts_new = (
            current is None
            or not current["bullets"]
            or has_header_separator
            or bool(dates)
            or bool(_project_url(line))
            or previous_bullet_closed
            or (
                current is not None
                and bool(current["bullets"])
                and next_is_bullet
                and _looks_like_project_title(line)
            )
        )
        # A wrapped bullet that starts a new line is usually lowercase or a
        # sentence fragment; keep it with the bullet when the previous bullet
        # is still open.
        if (
            previous_was_bullet
            and not has_header_separator
            and not dates
            and not _project_url(line)
            and not previous_bullet_closed
            and not next_is_bullet
        ):
            starts_new = False

        if starts_new:
            current = start_entry(line)
            entries.append(current)
            previous_was_bullet = False
            continue

        if current is not None and current["bullets"]:
            current["bullets"][-1] = (
                current["bullets"][-1].rstrip() + " " + line.strip()
            ).strip()
        previous_was_bullet = False

    return entries


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


def _merge_education(
    base: list[dict[str, Any]], override: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge parsed education into the existing rows field by field.

    A PDF re-import must not erase curated data the parser cannot see (for
    example a resume-forge course list), so existing values survive unless the
    parsed row actually supplies a replacement.
    """
    def key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    existing = {key(entry.get("institution")): dict(entry) for entry in base}
    merged: list[dict[str, Any]] = []
    for parsed in override:
        current = existing.get(key(parsed.get("institution")))
        if current is None:
            merged.append(dict(parsed))
            continue
        for field, value in parsed.items():
            if value not in (None, "", [], {}):
                current[field] = value
        if not current.get("courses") and parsed.get("courses"):
            current["courses"] = list(parsed["courses"])
        merged.append(current)
    return merged


def education_with_resume_courses(profile: Profile) -> list[dict[str, Any]]:
    """Return education rows, filling missing courses from the source resume.

    Profiles imported before course parsing existed still have an accurate
    resume_path. This is a read-only fallback: it never writes to the profile,
    so a tailored resume and the profile page can show the courses the original
    PDF actually lists.
    """
    entries = [
        dict(entry) for entry in (profile.education or []) if isinstance(entry, dict)
    ]
    if not entries or all(entry.get("courses") for entry in entries):
        return entries
    if not profile.resume_path:
        return entries
    path = Path(profile.resume_path)
    if not path.is_file():
        return entries
    try:
        parsed = profile_from_resume(path)
    except Exception:  # noqa: BLE001 - display/render fallback only
        return entries

    def key(value: Any) -> str:
        return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()

    by_institution = {key(entry.get("institution")): entry for entry in parsed.education}
    for entry in entries:
        if entry.get("courses"):
            continue
        match = by_institution.get(key(entry.get("institution")))
        if match and match.get("courses"):
            entry["courses"] = list(match["courses"])
    return entries


def merge_profiles(base: Profile, override: Profile) -> Profile:
    merged = Profile.from_dict(base.to_dict())
    education = _merge_education(base.education, override.education)
    for field_name in Profile.__dataclass_fields__:
        if field_name == "education":
            continue
        value = getattr(override, field_name)
        if value not in (None, "", [], {}):
            setattr(merged, field_name, value)
    if override.education:
        merged.education = education
    return merged
