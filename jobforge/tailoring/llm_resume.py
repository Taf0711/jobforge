"""LLM-first resume generation with automated deterministic grounding checks.

The model may select, reorder, shorten, and rephrase verified source material.
It may not invent employers, titles, dates, degrees, certifications, metrics,
skills, projects, or URLs. Every generated bullet must cite source IDs from the
same entry, and a deterministic verifier rejects unsupported output before the
Typst renderer sees it.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from ..matching.archetypes import detect_archetype
from ..matching.jd import JD
from ..matching.skills import extract_skills
from ..models import Job, Profile

NUMBER = re.compile(r"\b\d[\d,.]*\b")
STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "your", "you", "our",
    "are", "will", "have", "has", "was", "were", "not", "but", "all", "any",
    "can", "may", "who", "what", "work", "working", "role", "team", "teams",
    "company", "job", "experience", "including", "using", "build", "built",
    "help", "across", "into", "more", "than", "about", "their", "while",
    "also", "using", "used", "with", "within", "over", "under", "through",
}
MAX_BULLETS = 4
MAX_EXPERIENCE = 3
MAX_PROJECTS = 3
MAX_SKILLS = 40
MAX_TEXT = 360

LLM_RESUME_SYSTEM = """You are JobForge's resume editor. Return JSON only.

You receive a verified candidate profile, source facts with IDs, and one job.
Build a tailored one-page resume that leads with the most relevant verified
work.

You may select, reorder, shorten, and rephrase. You must not fabricate.

Hard rules:
1. Use only source items provided. Do not add an employer, title, date, degree,
   certification, project, URL, metric, or skill that is not in the source.
2. Copy employer, title, company, start/end dates, project name, tech line, and
   URL exactly from the source. Do not rewrite dates or employers.
3. Every bullet must cite one or more source_ids from the same experience or
   project. The cited source texts must contain every number in the bullet.
4. A bullet may mention only skills in allowed_skills.
5. The summary must cite source_ids and may only use numbers/skills from those
   sources (or the general fact bank).
6. Prefer 2-4 bullets per entry, at most 3 experience entries, at most 3
   projects, and at most 40 skills.
7. Keep bullets ATS-friendly: plain text, no tables, no columns, no markdown.
8. If a source is weak or irrelevant, omit it rather than inventing.

Return exactly this shape:
{
  "summary": {"text": "...", "source_ids": ["..."]},
  "experience": [
    {"source_key": "exp:0",
     "bullets": [{"text": "...", "source_ids": ["..."]}]}
  ],
  "projects": [
    {"source_key": "proj:0",
     "bullets": [{"text": "...", "source_ids": ["..."]}]}
  ],
  "skill_groups": [
    {"label": "Languages", "items": ["Python", "Go"]}
  ]
}
"""


@dataclass
class SourceItem:
    id: str
    text: str
    skills: list[str]
    entry_key: str
    section: str


@dataclass
class LLMResumeOutcome:
    data: dict[str, Any] | None
    warnings: list[str]
    source_ids: list[str]
    used: bool
    error: str = ""


def _words(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9+#.-]{2,}", text or "")
        if token.lower() not in STOPWORDS
    }


def _numbers(text: str) -> set[str]:
    return set(NUMBER.findall(text or ""))


def _entry_key_for_fact(
    fact: dict[str, Any], profile: Profile
) -> str:
    section = str(fact.get("section") or "").strip().lower()
    title = str(fact.get("title") or fact.get("name") or "").strip().lower()
    company = str(fact.get("company") or "").strip().lower()
    if section in {"projects", "project"}:
        for index, entry in enumerate(profile.projects):
            name = str(entry.get("title") or entry.get("name") or "").strip().lower()
            if name and (name == title or name == company):
                return f"proj:{index}"
        return "projects"
    if section in {"experience", "work", "employment"}:
        for index, entry in enumerate(profile.experience):
            entry_title = str(entry.get("title") or "").strip().lower()
            entry_company = str(entry.get("company") or "").strip().lower()
            if title and title == entry_title and (not company or company == entry_company):
                return f"exp:{index}"
            if company and company == entry_company and not title:
                return f"exp:{index}"
        return "experience"
    return "general"


def build_source_items(profile: Profile) -> list[SourceItem]:
    items: list[SourceItem] = []
    # Entry-scoped identity: identical text under two employers/projects is two
    # independently citable facts, not one fact.
    seen: set[tuple[str, str]] = set()

    def add(item: SourceItem) -> None:
        key = (item.entry_key, re.sub(r"\s+", " ", item.text).strip().lower())
        if not key[1]:
            return
        if key in seen:
            return
        seen.add(key)
        items.append(item)

    for index, fact in enumerate(profile.facts or []):
        if not isinstance(fact, dict):
            continue
        text = str(fact.get("text") or "").strip()
        if not text:
            continue
        fact_id = str(fact.get("id") or f"fact:{index}")
        add(
            SourceItem(
                id=fact_id,
                text=text,
                skills=[str(skill) for skill in (fact.get("skills") or []) if str(skill).strip()],
                entry_key=_entry_key_for_fact(fact, profile),
                section=str(fact.get("section") or ""),
            )
        )

    for index, entry in enumerate(profile.experience or []):
        for bullet_index, bullet in enumerate(entry.get("bullets") or []):
            text = str(bullet or "").strip()
            if text:
                add(
                    SourceItem(
                        id=f"exp:{index}:b:{bullet_index}",
                        text=text,
                        skills=[str(skill) for skill in extract_skills(text)],
                        entry_key=f"exp:{index}",
                        section="experience",
                    )
                )

    for index, entry in enumerate(profile.projects or []):
        for bullet_index, bullet in enumerate(entry.get("bullets") or []):
            text = str(bullet or "").strip()
            if text:
                add(
                    SourceItem(
                        id=f"proj:{index}:b:{bullet_index}",
                        text=text,
                        skills=[str(skill) for skill in extract_skills(text)],
                        entry_key=f"proj:{index}",
                        section="projects",
                    )
                )
    return items


def _allowed_skills(profile: Profile, items: list[SourceItem]) -> set[str]:
    allowed = {str(skill) for skill in (profile.skills or []) if str(skill).strip()}
    for item in items:
        allowed.update(item.skills)
    allowed.update(str(skill) for skill in extract_skills(" ".join(item.text for item in items)))
    return allowed


def _payload(
    profile: Profile,
    job: Job,
    jd: JD,
    items: list[SourceItem],
    archetypes: dict[str, dict[str, Any]] | None,
) -> dict[str, Any]:
    by_entry: dict[str, list[SourceItem]] = {}
    for item in items:
        by_entry.setdefault(item.entry_key, []).append(item)
    archetype = detect_archetype(jd, archetypes or {})
    return {
        "candidate": {
            "name": profile.full_name,
            "location": profile.location,
            "summary": profile.summary,
            "skills": list(profile.skills or []),
            "links": dict(profile.links or {}),
        },
        "experience": [
            {
                "source_key": f"exp:{index}",
                "title": entry.get("title", ""),
                "company": entry.get("company", ""),
                "location": entry.get("location", ""),
                "start": entry.get("start", ""),
                "end": entry.get("end", ""),
                "source_facts": [
                    {"source_id": item.id, "text": item.text}
                    for item in by_entry.get(f"exp:{index}", [])
                ],
            }
            for index, entry in enumerate(profile.experience or [])
        ],
        "projects": [
            {
                "source_key": f"proj:{index}",
                "name": entry.get("title") or entry.get("name") or "",
                "tech_line": entry.get("tech_line") or entry.get("description") or "",
                "url": entry.get("url", ""),
                "source_facts": [
                    {"source_id": item.id, "text": item.text}
                    for item in by_entry.get(f"proj:{index}", [])
                ],
            }
            for index, entry in enumerate(profile.projects or [])
            if not entry.get("dead")
        ],
        "general_facts": [
            {"source_id": item.id, "text": item.text}
            for item in items
            if item.entry_key in {"general", "experience", "projects"}
        ][:120],
        "education": profile.education,
        "certifications": profile.certifications,
        "allowed_skills": sorted(_allowed_skills(profile, items)),
        "job": {
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "description": (job.description or "")[:12000],
            "must_skills": list(jd.must_skills),
            "skills": list(jd.skills),
        },
        "archetype": (
            {
                "name": archetype.name,
                "label": archetype.label,
                "strongest_projects": list(
                    (archetype.spec.get("strongest_projects") or [])
                )[:5],
            }
            if archetype
            else None
        ),
    }


def _valid_text(
    text: str,
    source_texts: list[str],
    allowed_skills: set[str],
) -> bool:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text or len(text) > MAX_TEXT:
        return False
    source_blob = " ".join(source_texts)
    if not source_texts:
        return False
    if not _numbers(text) <= _numbers(source_blob):
        return False
    mentioned = set(extract_skills(text))
    if mentioned - allowed_skills:
        return False
    if not (_words(text) & _words(source_blob)):
        return False
    return True


def _source_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return []


def _bullet_entries(value: Any) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    if not isinstance(value, list):
        return out
    for bullet in value:
        if isinstance(bullet, dict):
            text = str(bullet.get("text") or "")
            ids = _source_ids(bullet.get("source_ids") or bullet.get("source_fact_ids"))
        else:
            text = str(bullet or "")
            ids = []
        out.append((text, ids))
    return out


def _validate_entry_bullets(
    raw_bullets: Any,
    source_items: list[SourceItem],
    allowed_skills: set[str],
    warnings: list[str],
    label: str,
) -> list[str]:
    by_id = {item.id: item for item in source_items}
    valid: list[str] = []
    seen: set[str] = set()
    for text, ids in _bullet_entries(raw_bullets):
        canonical = re.sub(r"\s+", " ", text).strip()
        if canonical in seen:
            continue
        ids = list(dict.fromkeys(ids))
        if not ids:
            # A plain string is acceptable only if it exactly matches a source
            # bullet; otherwise it has no provenance.
            match = next(
                (item for item in source_items if item.text.strip() == canonical),
                None,
            )
            if not match:
                warnings.append(f"Dropped uncited {label} bullet.")
                continue
            ids = [match.id]
        cited = [by_id[i] for i in ids if i in by_id]
        if len(cited) != len(ids):
            warnings.append(f"Dropped {label} bullet with unknown source id.")
            continue
        if not _valid_text(canonical, [item.text for item in cited], allowed_skills):
            warnings.append(f"Dropped unsupported {label} bullet.")
            continue
        seen.add(canonical)
        valid.append(canonical)
        if len(valid) >= MAX_BULLETS:
            break
    return valid


def generate_llm_resume(
    profile: Profile,
    job: Job,
    jd: JD,
    llm: Any,
    baseline: dict[str, Any],
    archetypes: dict[str, dict[str, Any]] | None = None,
) -> LLMResumeOutcome:
    items = build_source_items(profile)
    if not items:
        return LLMResumeOutcome(
            data=None,
            warnings=["No verified source facts; using deterministic resume."],
            source_ids=[],
            used=False,
            error="no source facts",
        )
    allowed_skills = _allowed_skills(profile, items)
    payload = _payload(profile, job, jd, items, archetypes)
    prompt = (
        "Build the tailored resume JSON from this verified profile.\n\n"
        + "SOURCE:\n"
        + _json_dumps(payload)
    )
    try:
        raw = llm.json_chat(LLM_RESUME_SYSTEM, prompt, max_tokens=16_000)
    except Exception as exc:  # noqa: BLE001 - provider errors are user-facing
        return LLMResumeOutcome(
            data=None,
            warnings=[f"LLM resume generation failed: {str(exc)[:240]}"],
            source_ids=[],
            used=False,
            error=str(exc),
        )
    if not isinstance(raw, dict):
        return LLMResumeOutcome(
            data=None,
            warnings=["LLM returned no usable resume JSON."],
            source_ids=[],
            used=False,
            error="invalid JSON",
        )

    warnings: list[str] = []
    data = copy.deepcopy(baseline)
    used = False
    source_ids: list[str] = []

    # Summary
    summary_raw = raw.get("summary")
    if isinstance(summary_raw, dict):
        summary_text = str(summary_raw.get("text") or "")
        summary_ids = _source_ids(summary_raw.get("source_ids") or summary_raw.get("source_fact_ids"))
    else:
        summary_text = str(summary_raw or "")
        summary_ids = []
    item_by_id = {item.id: item for item in items}
    summary_cited = [item_by_id[i] for i in summary_ids if i in item_by_id]
    if summary_ids and len(summary_cited) != len(summary_ids):
        warnings.append("Summary cited unknown source ids; kept profile summary.")
    elif summary_text and (
        _valid_text(summary_text, [item.text for item in summary_cited], allowed_skills)
        if summary_cited
        else _valid_text(
            summary_text,
            [item.text for item in items if item.entry_key == "general"] or [item.text for item in items],
            allowed_skills,
        )
    ):
        data["summary"] = re.sub(r"\s+", " ", summary_text).strip()
        source_ids.extend(item.id for item in summary_cited)
        used = True
    elif summary_text:
        warnings.append("Summary was not supported by cited facts; kept profile summary.")

    # Experience
    source_by_entry: dict[str, list[SourceItem]] = {}
    for item in items:
        source_by_entry.setdefault(item.entry_key, []).append(item)
    sample_by_key = {
        f"exp:{index}": entry for index, entry in enumerate(profile.experience or [])
    }
    experience_out: list[dict[str, Any]] = []
    for entry in raw.get("experience") or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("source_key") or entry.get("key") or "")
        source_entry = sample_by_key.get(key)
        if not source_entry:
            warnings.append("Dropped experience entry with unknown source key.")
            continue
        entry_items = source_by_entry.get(key, [])
        bullets = _validate_entry_bullets(
            entry.get("bullets"),
            entry_items,
            allowed_skills,
            warnings,
            "experience",
        )
        if not bullets:
            continue
        # Metadata always comes from the source, never from the model.
        experience_out.append(
            {
                "title": source_entry.get("title", ""),
                "company": source_entry.get("company", ""),
                "location": source_entry.get("location", ""),
                "start": source_entry.get("start", ""),
                "end": source_entry.get("end", ""),
                "bullets": bullets,
            }
        )
        source_ids.extend(
            item.id
            for item in entry_items
            if any(_words(item.text) & _words(bullet) for bullet in bullets)
        )
        if len(experience_out) >= MAX_EXPERIENCE:
            break
    if experience_out:
        data["experience"] = experience_out
        used = True
    elif raw.get("experience"):
        warnings.append("No supported experience entries; kept deterministic experience.")

    # Projects
    project_by_key = {
        f"proj:{index}": entry for index, entry in enumerate(profile.projects or [])
        if not entry.get("dead")
    }
    projects_out: list[dict[str, Any]] = []
    for entry in raw.get("projects") or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("source_key") or entry.get("key") or "")
        source_entry = project_by_key.get(key)
        if not source_entry:
            warnings.append("Dropped project with unknown source key.")
            continue
        entry_items = source_by_entry.get(key, [])
        bullets = _validate_entry_bullets(
            entry.get("bullets"),
            entry_items,
            allowed_skills,
            warnings,
            "project",
        )
        if not bullets:
            continue
        projects_out.append(
            {
                "name": source_entry.get("title") or source_entry.get("name") or "",
                "tech_line": source_entry.get("tech_line") or source_entry.get("description") or "",
                "url": source_entry.get("url", ""),
                "bullets": bullets,
            }
        )
        source_ids.extend(
            item.id
            for item in entry_items
            if any(_words(item.text) & _words(bullet) for bullet in bullets)
        )
        if len(projects_out) >= MAX_PROJECTS:
            break
    if projects_out:
        data["projects"] = projects_out
        used = True
    elif raw.get("projects"):
        warnings.append("No supported projects; kept deterministic projects.")

    # Skills
    groups: list[dict[str, Any]] = []
    canonical = {skill.lower(): skill for skill in allowed_skills}
    for group in raw.get("skill_groups") or []:
        if not isinstance(group, dict):
            continue
        items_out: list[str] = []
        for value in group.get("items") or []:
            skill = canonical.get(str(value).strip().lower())
            if skill and skill not in items_out:
                items_out.append(skill)
        if items_out:
            label = re.sub(r"\s+", " ", str(group.get("label") or "Skills")).strip()[:40]
            groups.append({"label": label, "items": items_out[:MAX_SKILLS]})
        if sum(len(group["items"]) for group in groups) >= MAX_SKILLS:
            break
    if groups:
        data["skill_groups"] = groups
        data["skills"] = [item for group in groups for item in group["items"]]
        used = True
    elif raw.get("skill_groups"):
        warnings.append("No supported skill groups; kept deterministic skills.")

    if not used:
        warnings.append("LLM resume produced no supported sections; using deterministic resume.")
        return LLMResumeOutcome(
            data=None,
            warnings=warnings,
            source_ids=[],
            used=False,
            error="no supported sections",
        )
    return LLMResumeOutcome(
        data=data,
        warnings=warnings,
        source_ids=list(dict.fromkeys(source_ids)),
        used=True,
    )


def _json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, default=str)
