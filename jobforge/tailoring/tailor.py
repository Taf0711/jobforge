"""Truthful resume tailoring: select, reorder, optionally rephrase, then render."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import Job, Profile, slugify
from ..matching.jd import JD, parse_jd
from ..matching.skills import GRAPH_PATH, SkillGraph
from .ats_check import ats_check
from .llm import COVER_LETTER_SYSTEM, GROUNDING_SYSTEM, LLMClient

TEMPLATE = Path(__file__).with_name("template.typ")
NUMBER = re.compile(r"\b\d[\d,.]*\b")
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "you", "our", "are", "will",
    "have", "has", "was", "were", "not", "but", "all", "any", "can", "may", "who", "what",
    "work", "working", "role", "team", "teams", "company", "job", "experience", "including",
    "using", "build", "built", "help", "across", "into", "more", "than", "about", "their",
}


@dataclass
class TailorResult:
    pdf_path: str
    typ_path: str
    data_path: str
    cover_letter_path: str = ""
    ats: dict[str, Any] = field(default_factory=dict)
    selected_fact_ids: list[str] = field(default_factory=list)
    summary: str = ""
    warnings: list[str] = field(default_factory=list)


def _keywords(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{2,}", text)
        if token.lower() not in STOPWORDS
    }


def _entry_key(entry: dict[str, Any]) -> tuple[str, str, str]:
    return (str(entry.get("title", "")), str(entry.get("company", "")), str(entry.get("start", "")))


def select_facts(profile: Profile, jd: JD, max_per_entry: int = 4) -> tuple[list[dict[str, Any]], list[str]]:
    """Choose the most relevant facts per entry while preserving original order."""
    jd_skills = set(jd.skills) | set(jd.must_skills)
    jd_words = _keywords(jd.description)
    facts = profile.facts or []

    by_entry: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for index, fact in enumerate(facts):
        key = (str(fact.get("title", "")), str(fact.get("company", "")), str(fact.get("section", "")))
        item = dict(fact)
        item["_index"] = index
        item["_score"] = 3 * len(set(fact.get("skills", []) or []) & jd_skills) + len(
            _keywords(fact.get("text", "")) & jd_words
        )
        by_entry.setdefault(key, []).append(item)

    selected: list[dict[str, Any]] = []
    for items in by_entry.values():
        ranked = sorted(items, key=lambda x: (-x["_score"], x["_index"]))
        chosen = ranked[:max_per_entry]
        if len(chosen) < min(2, len(items)):
            chosen = ranked[: min(2, len(items))]
        chosen.sort(key=lambda x: x["_index"])
        selected.extend(chosen)

    selected.sort(key=lambda x: x["_index"])
    return selected, [str(f.get("id", "")) for f in selected if f.get("id")]


def ordered_skills(profile: Profile, jd: JD) -> list[str]:
    must = set(jd.must_skills)
    nice = set(jd.skills)
    graph = SkillGraph(GRAPH_PATH)
    owned = set(profile.skills)

    def score(skill: str) -> tuple[int, int]:
        if skill in must:
            return (0, 0)
        if skill in nice:
            return (1, 0)
        for wanted in must:
            credit, _ = graph.partial_credit(wanted, {skill})
            if credit >= 0.5:
                return (2, 0)
        return (3, 1 if skill in owned else 2)

    return sorted(dict.fromkeys(profile.skills), key=lambda s: (score(s), s.lower()))


def _format_certification(cert: dict[str, Any]) -> str:
    name = str(cert.get("name", "")).strip()
    issuer = str(cert.get("issuer", "")).strip()
    year = str(cert.get("year", "")).strip()
    if issuer and year:
        return f"{name} — {issuer} ({year})"
    if issuer:
        return f"{name} — {issuer}"
    if year:
        return f"{name} ({year})"
    return name


def build_data(profile: Profile, jd: JD, selected: list[dict[str, Any]], summary: str = "") -> dict[str, Any]:
    contact = [
        profile.email,
        profile.phone,
        profile.location,
        profile.links.get("linkedin", ""),
        profile.links.get("github", ""),
        profile.links.get("portfolio", ""),
    ]
    selected_by_entry: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for fact in selected:
        key = (str(fact.get("title", "")), str(fact.get("company", "")), str(fact.get("section", "")))
        selected_by_entry.setdefault(key, []).append(fact)

    experience = []
    for entry in profile.experience:
        key = (str(entry.get("title", "")), str(entry.get("company", "")), "experience")
        bullets = [f["text"] for f in selected_by_entry.get(key, [])]
        if not bullets:
            bullets = list(entry.get("bullets", []))[:4]
        experience.append(
            {
                "title": entry.get("title", ""),
                "company": entry.get("company", ""),
                "location": entry.get("location", ""),
                "start": entry.get("start", ""),
                "end": entry.get("end", ""),
                "bullets": bullets,
            }
        )

    projects = []
    for entry in profile.projects:
        key = (str(entry.get("title", "")), str(entry.get("company", "")), "projects")
        bullets = [f["text"] for f in selected_by_entry.get(key, [])]
        if not bullets:
            bullets = list(entry.get("bullets", []))[:3]
        projects.append(
            {
                "name": entry.get("title", "") or entry.get("name", ""),
                "description": entry.get("company", ""),
                "bullets": bullets,
            }
        )

    education = [
        {
            "institution": e.get("institution", ""),
            "degree": e.get("degree", ""),
            "major": e.get("major", ""),
            "start": e.get("start", ""),
            "end": e.get("end", ""),
        }
        for e in profile.education
    ]

    certifications = [
        _format_certification(c) for c in profile.certifications if isinstance(c, dict)
    ]
    certifications = [c for c in certifications if c]

    return {
        "name": profile.full_name or "Your Name",
        "contact": [c for c in contact if c],
        "summary": summary or profile.summary,
        "experience": experience,
        "projects": projects,
        "education": education,
        "certifications": certifications,
        "skills": ordered_skills(profile, jd),
    }


def _supported(sentence: str, facts: list[str]) -> bool:
    """Reject numbers not present in the fact bank. Proper nouns are too noisy to police perfectly."""
    if not sentence.strip():
        return False
    known_numbers = set()
    for fact in facts:
        known_numbers.update(NUMBER.findall(fact))
    for number in NUMBER.findall(sentence):
        if number not in known_numbers:
            return False
    return True


def summarize(profile: Profile, job: Job, jd: JD, llm: LLMClient | None) -> str:
    base = profile.summary.strip()
    if not llm or not llm.available:
        return base
    facts = "\n".join(f"- [{f.get('id')}] {f.get('text')}" for f in profile.facts[:20])
    prompt = (
        f"Job title: {job.title}\nCompany: {job.company}\n"
        f"Job skills: {', '.join(jd.must_skills[:12])}\n\n"
        f"Candidate facts:\n{facts}\n\n"
        "Write a 2-sentence resume summary targeting this job. Use only the facts above. "
        "Do not add numbers, employers, or skills that are not in the facts. Return plain text."
    )
    out = llm.chat(GROUNDING_SYSTEM, prompt, temperature=0.1)
    if not out:
        return base
    out = out.strip().strip('"')
    if not _supported(out, [str(f.get("text", "")) for f in profile.facts]):
        return base
    return out


def cover_letter(profile: Profile, job: Job, jd: JD, llm: LLMClient | None) -> str:
    facts = "\n".join(f"- {f.get('text')}" for f in profile.facts[:12])
    if llm and llm.available:
        prompt = (
            f"Role: {job.title} at {job.company}\n"
            f"Top requirements: {', '.join(jd.must_skills[:10])}\n"
            f"Candidate: {profile.full_name}\nFacts:\n{facts}\n\n"
            "Write the cover letter now."
        )
        out = llm.chat(COVER_LETTER_SYSTEM, prompt, temperature=0.25)
        if out and _supported(out, [str(f.get("text", "")) for f in profile.facts] + [profile.full_name]):
            return out.strip()
    # Deterministic fallback: no invented claims.
    skills = ", ".join(jd.must_skills[:5]) or "the role"
    return (
        f"Dear Hiring Team,\n\n"
        f"I am applying for the {job.title} role at {job.company}. My background includes "
        f"{skills}, and my resume details the work I have done with them.\n\n"
        f"I would welcome the chance to discuss how my experience maps to your needs.\n\n"
        f"Sincerely,\n{profile.full_name}"
    )


def render(data: dict[str, Any], out_dir: Path) -> tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    data_path = out_dir / "data.json"
    typ_path = out_dir / "resume.typ"
    pdf_path = out_dir / "resume.pdf"
    data_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(TEMPLATE, typ_path)
    result = subprocess.run(
        ["typst", "compile", str(typ_path.name), str(pdf_path.name)],
        cwd=out_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Typst compile failed: {result.stderr.strip()[:1000]}")
    return pdf_path, typ_path, data_path


def tailor_resume(
    profile: Profile,
    job: Job,
    output_root: str | Path,
    *,
    llm: LLMClient | None = None,
    write_cover_letter: bool = True,
) -> TailorResult:
    jd = parse_jd(job.description or job.title, title_hint=job.title, company_hint=job.company, location_hint=job.location)
    selected, fact_ids = select_facts(profile, jd)
    summary = summarize(profile, job, jd, llm)
    data = build_data(profile, jd, selected, summary=summary)
    out_dir = Path(output_root) / slugify(job.company) / slugify(job.title, 60)
    pdf_path, typ_path, data_path = render(data, out_dir)

    cover_path = ""
    if write_cover_letter:
        text = cover_letter(profile, job, jd, llm)
        cover_path_obj = out_dir / "cover_letter.txt"
        cover_path_obj.write_text(text, encoding="utf-8")
        cover_path = str(cover_path_obj)

    ats = ats_check(pdf_path)
    warnings = list(ats.get("warnings", []))
    if not profile.facts:
        warnings.append("No fact bank found; bullets were copied from the parsed resume without selection")
    return TailorResult(
        pdf_path=str(pdf_path),
        typ_path=str(typ_path),
        data_path=str(data_path),
        cover_letter_path=cover_path,
        ats=ats,
        selected_fact_ids=fact_ids,
        summary=summary,
        warnings=warnings,
    )
