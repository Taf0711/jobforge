"""Truthful resume tailoring: select, reorder, optionally rephrase, then render."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess

import pymupdf
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import Job, Profile, slugify
from ..matching.archetypes import ArchetypeMatch, detect_archetype
from ..matching.jd import JD, parse_jd
from ..matching.skills import GRAPH_PATH, SkillGraph, extract_skills
from .ats_check import ats_check
from .llm import COVER_LETTER_SYSTEM, GROUNDING_SYSTEM, LLMClient

TEMPLATE = Path(__file__).with_name("template.typ")
COMPACT_TEMPLATE = Path(__file__).with_name("template_compact.typ")
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


DOMAIN_KEYWORDS = {
    "systems": {
        "systems", "distributed", "distributed-systems", "query engine", "database",
        "storage", "compiler", "low-level", "embedded", "hpc", "performance",
        "latency", "networking", "operating systems", "c++", "rust",
    },
    "data": {
        "data", "data-engineering", "etl", "pipeline", "sql", "spark", "analytics",
        "warehouse", "data-quality", "data structures",
    },
    "ai": {
        "ai", "ml", "llm", "machine learning", "deep learning", "agent", "agentic",
        "rag", "nlp", "pytorch", "inference", "model", "computer-vision",
    },
    "security": {
        "security", "iam", "identity", "access-control", "compliance", "auth",
        "rbac", "policy", "cryptography",
    },
    "fullstack": {
        "full-stack", "frontend", "backend", "react", "typescript", "javascript",
        "web", "api", "product", "rest",
    },
    "cloud": {
        "cloud", "infrastructure", "devops", "kubernetes", "docker", "aws", "gcp",
        "azure", "platform", "serverless",
    },
}


def _text_domains(text: str) -> set[str]:
    lowered = (text or "").lower()

    def has(keyword: str) -> bool:
        if len(keyword) <= 3:
            return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", lowered) is not None
        return keyword in lowered

    return {
        domain
        for domain, keywords in DOMAIN_KEYWORDS.items()
        if any(has(keyword) for keyword in keywords)
    }


def select_projects(
    profile: Profile,
    jd: JD,
    max_projects: int = 3,
    archetypes: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Choose the most JD-relevant projects while preserving profile order.

    A parsed resume usually contains three projects. The resume-forge project
    bank can contain a dozen or more, so rendering every project would produce a
    multi-page resume. Score tags, tech lines, best-for labels, and bullets
    against the JD, then keep the top N.
    """
    if len(profile.projects) <= max_projects:
        return list(profile.projects)

    match: ArchetypeMatch | None = detect_archetype(jd, archetypes or {})
    priority: dict[str, int] = {}
    if match:
        for rank, name in enumerate(match.spec.get("strongest_projects", []) or []):
            priority[str(name).strip().lower()] = rank

    jd_terms = set(jd.skills) | set(jd.must_skills) | _keywords(jd.description)
    jd_skills = set(jd.skills) | set(jd.must_skills)
    must = set(jd.must_skills)
    jd_domains = _text_domains(" ".join([*jd.skills, *jd.must_skills]))
    title_domains = _text_domains(jd.title)
    scored: list[tuple[int, int, dict[str, Any]]] = []
    for index, project in enumerate(profile.projects):
        text = " ".join(
            str(project.get(key, "") or "")
            for key in ("name", "title", "company", "description", "tech_line")
        )
        for key in ("tags", "best_for", "bullets"):
            text += " " + " ".join(str(item) for item in project.get(key, []) or [])
        skills = set(extract_skills(text))
        signals = _keywords(
            " ".join(
                str(item)
                for key in ("tags", "best_for", "tech_line")
                for item in (project.get(key, []) or [])
            )
        )
        project_domains = _text_domains(
            " ".join(
                str(item)
                for key in ("tags", "best_for", "tech_line")
                for item in (project.get(key, []) or [])
            )
        )
        # Technical skills dominate; domain labels then generic prose overlap.
        score = (
            5 * len(skills & jd_skills)
            + 3 * len(skills & must)
            + 8 * len(project_domains & title_domains)
            + 4 * len(project_domains & jd_domains)
            + min(3, len(signals & jd_terms))
        )
        project_name = str(project.get("title", "") or project.get("name", "")).strip().lower()
        if project_name in priority:
            score += 10_000 - priority[project_name] * 100
        scored.append((score, index, project))

    chosen = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_projects]
    chosen_indices = {index for _, index, _ in chosen}
    return [project for index, project in enumerate(profile.projects) if index in chosen_indices]


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


def build_data(
    profile: Profile,
    jd: JD,
    selected: list[dict[str, Any]],
    summary: str = "",
    archetypes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
    for entry in select_projects(profile, jd, archetypes=archetypes):
        key = (str(entry.get("title", "")), str(entry.get("company", "")), "projects")
        bullets = [f["text"] for f in selected_by_entry.get(key, [])]
        if not bullets:
            bullets = list(entry.get("bullets", []))[:3]
        projects.append(
            {
                "name": entry.get("title", "") or entry.get("name", ""),
                "description": entry.get("description", "") or entry.get("company", ""),
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
        "skills": ordered_skills(profile, jd)[:24],
    }


def _profile_skills(profile: Profile) -> set[str]:
    skills = set(profile.skills or [])
    for fact in profile.facts:
        skills.update(str(skill) for skill in (fact.get("skills") or []))
    return skills


def _supported(
    sentence: str, facts: list[str], allowed_skills: set[str] | None = None
) -> bool:
    """Reject unsupported numbers and skills.

    Proper-noun and employer claims still need human review; this is a
    conservative floor, not a complete semantic proof.
    """
    if not sentence.strip():
        return False
    known_numbers = set()
    for fact in facts:
        known_numbers.update(NUMBER.findall(fact))
    for number in NUMBER.findall(sentence):
        if number not in known_numbers:
            return False
    if allowed_skills is not None:
        mentioned = set(extract_skills(sentence))
        if mentioned - set(allowed_skills):
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
    if not _supported(
        out,
        [str(f.get("text", "")) for f in profile.facts],
        allowed_skills=_profile_skills(profile),
    ):
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
        if out and _supported(
            out,
            [str(f.get("text", "")) for f in profile.facts] + [profile.full_name],
            allowed_skills=_profile_skills(profile),
        ):
            return out.strip()
    # Deterministic fallback: only claim skills the profile actually owns.
    owned = [skill for skill in jd.must_skills if skill in set(profile.skills)]
    if owned:
        background = (
            f"My background includes {', '.join(owned[:5])}, and my resume details "
            "the work I have done with them."
        )
    elif profile.skills:
        background = (
            f"My background includes {', '.join(profile.skills[:4])}, and my resume "
            "details the work I have done."
        )
    else:
        background = "My resume details the work I have done."
    focus = ""
    if not owned and jd.must_skills:
        focus = f" I am interested in the role's focus on {', '.join(jd.must_skills[:3])}."
    return (
        f"Dear Hiring Team,\n\n"
        f"I am applying for the {job.title} role at {job.company}. {background}{focus}\n\n"
        f"I would welcome the chance to discuss how my experience maps to your needs.\n\n"
        f"Sincerely,\n{profile.full_name}"
    )


def render(data: dict[str, Any], out_dir: Path, template: Path = TEMPLATE) -> tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    data_path = out_dir / "data.json"
    typ_path = out_dir / "resume.typ"
    pdf_path = out_dir / "resume.pdf"
    data_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    shutil.copyfile(template, typ_path)
    result = subprocess.run(
        ["typst", "compile", str(typ_path.name), str(pdf_path.name)],
        cwd=out_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Typst compile failed: {result.stderr.strip()[:1000]}")
    return pdf_path, typ_path, data_path


def _output_dir_name(job: Job) -> str:
    """Stable, collision-free directory; the job id disambiguates same titles."""
    base = slugify(job.title, 60)
    location = slugify(job.location, 24) if (job.location or "").strip() else ""
    if job.id is not None:
        ident = str(job.id)
    else:
        basis = job.apply_url or job.url or job.source_id or base
        ident = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:8]
    return "-".join(part for part in (base, location, ident) if part)


def tailor_resume(
    profile: Profile,
    job: Job,
    output_root: str | Path,
    *,
    llm: LLMClient | None = None,
    write_cover_letter: bool = True,
    archetypes: dict[str, dict[str, Any]] | None = None,
) -> TailorResult:
    missing = [
        field
        for field, value in (
            ("name", profile.full_name),
            ("email", profile.email),
            ("phone", profile.phone),
        )
        if not str(value or "").strip()
    ]
    if missing:
        raise ValueError(f"Profile is missing required contact fields: {', '.join(missing)}")
    jd = parse_jd(job.description or job.title, title_hint=job.title, company_hint=job.company, location_hint=job.location)
    selected, fact_ids = select_facts(profile, jd, max_per_entry=4)
    summary = summarize(profile, job, jd, llm)
    data = build_data(profile, jd, selected, summary=summary, archetypes=archetypes)
    out_dir = Path(output_root) / slugify(job.company) / _output_dir_name(job)
    template = COMPACT_TEMPLATE if len(profile.projects) > 3 else TEMPLATE
    pdf_path, typ_path, data_path = render(data, out_dir, template=template)
    ats = ats_check(pdf_path)
    if ats.get("pages", 0) > 1 and template != COMPACT_TEMPLATE:
        # Try the tighter layout before failing the one-page gate.
        pdf_path, typ_path, data_path = render(data, out_dir, template=COMPACT_TEMPLATE)
        ats = ats_check(pdf_path)

    cover_path = ""
    if write_cover_letter:
        text = cover_letter(profile, job, jd, llm)
        (out_dir / "cover_letter.txt").write_text(text, encoding="utf-8")
        # Most ATS upload controls accept PDF, not .txt. Keep the text for
        # editing and hand the PDF path to the application/fill layer.
        cover_pdf = out_dir / "cover_letter.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        rect = pymupdf.Rect(54, 54, page.rect.width - 54, page.rect.height - 54)
        page.insert_textbox(rect, text, fontsize=11, fontname="helv")
        doc.save(cover_pdf)
        doc.close()
        cover_path = str(cover_pdf)

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
