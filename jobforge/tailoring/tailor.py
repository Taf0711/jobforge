"""Truthful resume tailoring: select, reorder, optionally rephrase, then render."""

from __future__ import annotations

import copy
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
from .llm_resume import generate_llm_resume

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
    llm_used: bool = False
    llm_model: str = ""
    mode: str = "auto"


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
    live_projects = [project for project in profile.projects if not project.get("dead")]
    if len(live_projects) <= max_projects:
        return live_projects

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
    for index, project in enumerate(live_projects):
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
    return [project for index, project in enumerate(live_projects) if index in chosen_indices]


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


SKILL_GROUP_ORDER = ("Languages", "Technologies", "Concepts", "Developer Tools", "Additional")
SKILL_GROUP_MEMBERS: dict[str, tuple[str, ...]] = {
    "Languages": (
        "Python", "Go", "TypeScript", "JavaScript", "Java", "C/C++", "SQL", "C#",
        "HTML/CSS", "PHP", "Ruby", "Rust", "Kotlin", "Swift", "Scala", "R", "MATLAB",
        "Perl", "Bash", "C", "C++",
    ),
    "Technologies": (
        "Docker", "AWS", "FastAPI", "React", "Next.js", "Spring Boot", "Flask",
        "PostgreSQL", "MongoDB", "SQLite", ".NET Core", "RxJS", "Oracle PeopleSoft",
        "Drupal 10", "MySQL", "SQL Server", "Kubernetes", "Jenkins", "Streamlit",
        "Redis", "OPA", "Prometheus", "Grafana", "Terraform", "Django", "Node.js",
        "Express", "GraphQL", "Kafka", "RabbitMQ", "Airflow", "Snowflake", "Databricks",
        "Spark", "Cloud Foundry", "Lambda", "S3", "EC2", "OAuth", "SSO", "JWT",
        "Serverless", "DynamoDB", "AWS Lambda", "Entity Framework", "AngularJS",
        "Google Cloud", "Gemini API", "OpenCV", "YOLOv11", "WebGPU", "OPA/Rego",
        "NumPy", "scikit-learn",
    ),
    "Concepts": (
        "LLM Orchestration", "LLM", "AI Infrastructure", "Security", "Distributed Systems",
        "OOP", "DSA", "REST APIs", "CI/CD", "Cloud", "Data Analysis",
        "Operating Systems", "Networking", "Machine Learning", "Deep Learning", "NLP",
        "Computer Vision", "Reinforcement Learning", "System Design", "Testing",
        "Agile/SDLC", "Microservices", "Observability", "High-Performance Computing",
        "Concurrency", "Data Engineering", "ETL", "Accessibility", "Responsive Design",
        "Algorithms", "Data Structures", "Computer Graphics", "Full-stack Development",
        "Time-Series Forecasting", "Test Automation", "RBAC",
        "Identity and Access Management", "Edge Computing", "Parallel Computing",
        "Performance Engineering", "Data Structures & Algorithms", "RAG",
        "AI/ML Infrastructure", "TCP/IP",
    ),
    "Developer Tools": (
        "Git", "GitHub", "Linux", "Unix", "VIM", "Zed", "Claude Code", "Codex",
        "Bruno", "Jira", "Confluence", "tcpdump", "pytest", "Jest", "Playwright",
        "Selenium", "GitLab", "Figma",
    ),
}
SKILL_GROUP_LIMITS = {
    "Languages": 12, "Technologies": 19, "Concepts": 12, "Developer Tools": 10, "Additional": 4,
}
MONTHS = {
    "01": "January", "02": "February", "03": "March", "04": "April", "05": "May",
    "06": "June", "07": "July", "08": "August", "09": "September", "10": "October",
    "11": "November", "12": "December",
}


def _display_date(value: Any) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})", text)
    if match:
        return f"{MONTHS.get(match.group(2), match.group(2))} {match.group(1)}"
    return text


def _display_link(value: str) -> str:
    return re.sub(r"^https?://(?:www\.)?", "", (value or "").strip()).rstrip("/")


def _project_url(entry: dict[str, Any]) -> str:
    for key in ("url", "link", "repo", "github", "source_url"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value if value.startswith(("http://", "https://")) else "https://" + value
    haystack = " ".join(
        str(entry.get(key) or "")
        for key in ("notes", "description", "tech_line")
    ) + " " + " ".join(str(item) for item in entry.get("bullets", []) or [])
    match = re.search(r"(?:https?://)?github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", haystack)
    return f"https://{match.group(0).removeprefix('https://').removeprefix('http://')}" if match else ""


def _degree_line(entry: dict[str, Any]) -> str:
    degree = str(entry.get("degree") or "").strip()
    major = str(entry.get("major") or "").strip()
    if degree and major:
        return f"{degree} in {major}"
    return degree or major


def _skill_groups(profile: Profile, jd: JD) -> list[dict[str, Any]]:
    owned = set(profile.skills or [])
    if "C" in owned and "C++" in owned:
        owned.discard("C")
        owned.discard("C++")
        owned.add("C/C++")
    priority = set(jd.must_skills) | set(jd.skills)
    known: set[str] = set()
    grouped: list[dict[str, Any]] = []
    for label in SKILL_GROUP_ORDER:
        members = SKILL_GROUP_MEMBERS.get(label, ())
        known.update(members)
        items = [skill for skill in members if skill in owned]
        # Keep the refined resume-forge order, but pull exact JD terms forward.
        items = [skill for skill in items if skill in priority] + [
            skill for skill in items if skill not in priority
        ]
        items = items[: SKILL_GROUP_LIMITS[label]]
        if items:
            grouped.append({"label": label, "items": items})
    extras = sorted(owned - known)
    if extras:
        grouped.append({"label": "Additional", "items": extras[: SKILL_GROUP_LIMITS["Additional"]]})
    return grouped


def build_data(
    profile: Profile,
    jd: JD,
    selected: list[dict[str, Any]],
    summary: str = "",
    archetypes: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    selected_by_entry: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for fact in selected:
        key = (str(fact.get("title", "")), str(fact.get("company", "")), str(fact.get("section", "")))
        selected_by_entry.setdefault(key, []).append(fact)

    header: list[dict[str, str]] = []
    if profile.phone:
        header.append({"text": profile.phone, "url": ""})
    if profile.email:
        header.append({"text": profile.email, "url": f"mailto:{profile.email}"})
    for key in ("linkedin", "github", "portfolio"):
        value = str(profile.links.get(key) or "").strip()
        if value:
            header.append({"text": _display_link(value), "url": value})
    if profile.work_auth.get("us_citizen"):
        header.append({"text": "U.S. Citizen", "url": ""})

    experience = []
    # Keep the four most recent roles. Later entries (for example a
    # mentorship) stay in the profile but off the page; adaptive trimming
    # below removes bullets if the four entries still overflow one page.
    for entry in profile.experience[:4]:
        key = (str(entry.get("title", "")), str(entry.get("company", "")), "experience")
        bullets = [f["text"] for f in selected_by_entry.get(key, [])]
        if not bullets:
            bullets = list(entry.get("bullets", []))[:4]
        experience.append(
            {
                "title": entry.get("title", ""),
                "company": entry.get("company", ""),
                "location": entry.get("location", ""),
                "start": _display_date(entry.get("start")),
                "end": _display_date(entry.get("end")),
                "bullets": bullets,
            }
        )

    projects = []
    for entry in select_projects(profile, jd, archetypes=archetypes):
        key = (str(entry.get("title", "")), str(entry.get("company", "")), "projects")
        bullets = [f["text"] for f in selected_by_entry.get(key, [])][:3]
        if not bullets:
            bullets = list(entry.get("bullets", []))[:3]
        projects.append(
            {
                "name": entry.get("title", "") or entry.get("name", ""),
                "tech_line": entry.get("tech_line", "") or entry.get("description", "") or entry.get("company", ""),
                "url": _project_url(entry),
                "bullets": bullets,
            }
        )

    education = [
        {
            "institution": e.get("institution", ""),
            "degree": e.get("degree", ""),
            "major": e.get("major", ""),
            "degree_line": _degree_line(e),
            "location": e.get("location", "") or profile.location,
            "start": _display_date(e.get("start")),
            "end": _display_date(e.get("end") or e.get("graduation")),
            "courses": list(e.get("courses") or e.get("relevant_courses") or [])[:8],
        }
        for e in profile.education
    ]

    certifications = [
        _format_certification(c) for c in profile.certifications if isinstance(c, dict)
    ]
    certifications = [c for c in certifications if c]

    skill_groups = _skill_groups(profile, jd)
    return {
        "name": profile.full_name or "Your Name",
        "header": header,
        "contact": [item["text"] for item in header],
        "summary": summary or profile.summary,
        "experience": experience,
        "projects": projects,
        "education": education,
        "certifications": certifications,
        "skills": [item for group in skill_groups for item in group["items"]],
        "skill_groups": skill_groups,
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


def _output_dir_for(profile: Profile, job: Job, output_root: str | Path) -> Path:
    """Return a profile-scoped, job-scoped artifact directory.

    Job ids are global, but the same job can be tailored for more than one
    profile. The profile segment keeps one profile from overwriting another
    profile's resume at the same stable path.
    """
    ident = f"profile-{profile.id}" if profile.id is not None else "profile-unsaved"
    return Path(output_root) / ident / slugify(job.company) / _output_dir_name(job)


def _trim_render_data(
    data: dict[str, Any],
    *,
    experience_bullets: int,
    project_bullets: int,
    max_projects: int,
    max_groups: int,
    items_per_group: int,
) -> dict[str, Any]:
    """Return a verified-content subset that is more likely to fit one page.

    This only removes already-selected bullets/skills from the rendered view.
    It never adds, rewrites, or reattributes a fact.
    """
    trimmed = copy.deepcopy(data)
    trimmed["experience"] = [
        {**entry, "bullets": list(entry.get("bullets") or [])[:experience_bullets]}
        for entry in (trimmed.get("experience") or [])[:4]
    ]
    trimmed["projects"] = [
        {**entry, "bullets": list(entry.get("bullets") or [])[:project_bullets]}
        for entry in (trimmed.get("projects") or [])[:max_projects]
    ]
    groups = []
    for group in (trimmed.get("skill_groups") or [])[:max_groups]:
        items = list(group.get("items") or [])[:items_per_group]
        if items:
            groups.append({**group, "items": items})
    trimmed["skill_groups"] = groups
    trimmed["skills"] = [item for group in groups for item in group["items"]]
    return trimmed


def tailor_resume(
    profile: Profile,
    job: Job,
    output_root: str | Path,
    *,
    llm: LLMClient | None = None,
    write_cover_letter: bool = True,
    archetypes: dict[str, dict[str, Any]] | None = None,
    mode: str = "auto",
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
    baseline = build_data(profile, jd, selected, summary="", archetypes=archetypes)
    data = baseline
    mode = (mode or "auto").strip().lower()
    llm_used = False
    llm_source_ids: list[str] = []
    llm_warnings: list[str] = []
    llm_model = str(getattr(llm, "model", "") or "")
    if mode != "deterministic":
        if llm is None or not llm.available:
            if mode == "llm":
                raise ValueError(
                    "LLM tailoring is required but no model provider is available. "
                    "Connect a provider or set tailoring.mode to auto/deterministic."
                )
        else:
            outcome = generate_llm_resume(
                profile, job, jd, llm, baseline, archetypes=archetypes
            )
            llm_warnings = outcome.warnings
            if outcome.data is not None:
                data = outcome.data
                llm_used = True
                llm_source_ids = outcome.source_ids
            elif mode == "llm":
                detail = "; ".join(outcome.warnings) or (outcome.error or "unknown error")
                raise ValueError(f"LLM tailoring failed: {detail}")
    source_ids = llm_source_ids if llm_source_ids else fact_ids
    out_dir = _output_dir_for(profile, job, output_root)
    pdf_path, typ_path, data_path = render(data, out_dir, template=TEMPLATE)
    ats = ats_check(pdf_path)
    if ats.get("pages", 0) > 1:
        # Keep the same resume-forge visual language, only tighten spacing/font
        # enough to preserve the one-page requirement.
        pdf_path, typ_path, data_path = render(data, out_dir, template=COMPACT_TEMPLATE)
        ats = ats_check(pdf_path)
    if ats.get("pages", 0) > 1:
        # If the compact layout is still too tall, remove only already-selected
        # content in progressively tighter steps. Never synthesize a replacement
        # bullet or skill. The first candidate usually fits a full profile.
        for attempt in (
            (3, 3, 2, 4, 6),
            (3, 2, 2, 4, 6),
            (2, 2, 2, 3, 6),
            (2, 2, 2, 3, 5),
            (2, 1, 1, 2, 5),
            (1, 1, 1, 2, 4),
        ):
            candidate = _trim_render_data(
                data,
                experience_bullets=attempt[0],
                project_bullets=attempt[1],
                max_projects=attempt[2],
                max_groups=attempt[3],
                items_per_group=attempt[4],
            )
            candidate_pdf, candidate_typ, candidate_data = render(
                candidate, out_dir, template=COMPACT_TEMPLATE
            )
            candidate_ats = ats_check(candidate_pdf)
            if candidate_ats.get("pages", 0) <= 1:
                data = candidate
                pdf_path, typ_path, data_path = (
                    candidate_pdf,
                    candidate_typ,
                    candidate_data,
                )
                ats = candidate_ats
                break

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

    warnings = list(llm_warnings) + list(ats.get("warnings", []))
    if not profile.facts:
        warnings.append("No fact bank found; bullets were copied from the parsed resume without selection")
    return TailorResult(
        pdf_path=str(pdf_path),
        typ_path=str(typ_path),
        data_path=str(data_path),
        cover_letter_path=cover_path,
        ats=ats,
        selected_fact_ids=source_ids,
        summary=str(data.get("summary") or ""),
        warnings=warnings,
        llm_used=llm_used,
        llm_model=llm_model if llm_used else "",
        mode=mode,
    )
