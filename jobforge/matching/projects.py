"""Merge parsed resume projects into a curated project bank.

A resume upload or a targeted reparse should not silently discard projects
that were imported from resume-forge or added through the GitHub review flow.
The default ``merge`` mode updates projects with matching titles, appends new
projects, and preserves everything else. ``replace`` remains available for
users who want the resume to be the only source of truth.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

from .skills import extract_skills

PROJECT_MODES = {"merge", "replace"}


def normalize_project_mode(value: str | None, default: str = "merge") -> str:
    mode = (value or default).strip().lower()
    return mode if mode in PROJECT_MODES else default


def project_key(project: dict[str, Any]) -> str:
    title = str(project.get("title") or project.get("name") or "").strip()
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _url_key(project: dict[str, Any]) -> str:
    url = str(project.get("url") or "").strip().lower()
    return url.rstrip("/") if url else ""


def _bullet_list(project: dict[str, Any]) -> list[str]:
    return [
        str(item).strip()
        for item in (project.get("bullets") or [])
        if str(item).strip()
    ]


@dataclass
class ProjectMergeResult:
    projects: list[dict[str, Any]]
    project_facts: list[dict[str, Any]]
    mode: str
    added: int
    updated: int
    preserved: int


def _find_existing(
    parsed: dict[str, Any],
    existing: list[dict[str, Any]],
    by_title: dict[str, int],
    by_url: dict[str, int],
) -> int | None:
    key = project_key(parsed)
    if key and key in by_title:
        return by_title[key]
    url = _url_key(parsed)
    if url and url in by_url:
        return by_url[url]
    return None


def _update_project(existing: dict[str, Any], parsed: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(existing)
    title = str(parsed.get("title") or parsed.get("name") or "").strip()
    if title:
        merged["title"] = title
        merged["name"] = title
    old_tech = str(existing.get("tech_line") or "").strip()
    tech = str(parsed.get("tech_line") or "").strip()
    if tech:
        merged["tech_line"] = tech
        old_desc = str(existing.get("description") or "").strip()
        # resume-forge stores the tech line in both fields. Update that copy,
        # but leave a consciously curated description alone.
        if not old_desc or old_desc == old_tech:
            merged["description"] = tech
    parsed_url = str(parsed.get("url") or "").strip()
    if parsed_url:
        merged["url"] = parsed_url
    for field in ("start", "end"):
        value = str(parsed.get(field) or "").strip()
        if value:
            merged[field] = value
    bullets = _bullet_list(parsed)
    if bullets:
        merged["bullets"] = bullets
    return merged


def _project_facts(
    projects: list[dict[str, Any]],
    *,
    existing_facts: list[dict[str, Any]],
    preserved_keys: set[str],
    used_ids: set[str],
) -> list[dict[str, Any]]:
    existing_project_facts = [
        fact for fact in existing_facts if fact.get("section") == "projects"
    ]
    existing_by_key: dict[str, list[dict[str, Any]]] = {}
    for fact in existing_project_facts:
        key = project_key(fact)
        if key:
            existing_by_key.setdefault(key, []).append(copy.deepcopy(fact))

    # Facts belonging to projects that are being regenerated are replaced, so
    # their IDs may be reused. Keeping them reserved would append "-2", "-3",
    # ... on every reparse.
    replaced_ids = {
        str(fact.get("id"))
        for fact in existing_project_facts
        if fact.get("id") and project_key(fact) not in preserved_keys
    }
    reserved_ids = {str(item) for item in used_ids} - replaced_ids
    facts: list[dict[str, Any]] = []
    for index, project in enumerate(projects, start=1):
        key = project_key(project)
        if key in preserved_keys:
            kept = existing_by_key.get(key, [])
            if kept:
                facts.extend(kept)
                continue
        bullets = _bullet_list(project)
        if not bullets:
            continue
        title = str(project.get("title") or project.get("name") or "").strip()
        tech = str(project.get("tech_line") or project.get("description") or "")
        tags = " ".join(str(item) for item in project.get("tags") or [])
        company = str(project.get("company") or "")
        source_id = str(project.get("source_id") or "")
        for bullet_index, text in enumerate(bullets, start=1):
            base = f"resume-proj-{index:02d}-{bullet_index:02d}"
            fact_id = base
            suffix = 2
            while fact_id in reserved_ids:
                fact_id = f"{base}-{suffix}"
                suffix += 1
            reserved_ids.add(fact_id)
            facts.append(
                {
                    "id": fact_id,
                    "section": "projects",
                    "title": title,
                    "company": company,
                    "text": text,
                    "skills": extract_skills(f"{text} {tech} {tags}".strip()),
                    "source_id": source_id,
                }
            )
    return facts


def merge_project_bank(
    existing_projects: list[dict[str, Any]] | None,
    parsed_projects: list[dict[str, Any]] | None,
    *,
    existing_facts: list[dict[str, Any]] | None = None,
    mode: str = "merge",
) -> ProjectMergeResult:
    """Return the project bank and project facts after applying ``mode``."""
    mode = normalize_project_mode(mode)
    existing = copy.deepcopy(existing_projects or [])
    parsed = copy.deepcopy(parsed_projects or [])
    existing_facts = copy.deepcopy(existing_facts or [])

    if mode == "replace":
        projects = [project for project in parsed if project_key(project)]
        used_ids = {str(fact.get("id")) for fact in existing_facts if fact.get("id")}
        facts = _project_facts(
            projects,
            existing_facts=[],
            preserved_keys=set(),
            used_ids=used_ids,
        )
        return ProjectMergeResult(
            projects=projects,
            project_facts=facts,
            mode=mode,
            added=len(projects),
            updated=0,
            preserved=0,
        )

    by_title = {project_key(project): index for index, project in enumerate(existing)}
    by_url = {
        _url_key(project): index
        for index, project in enumerate(existing)
        if _url_key(project)
    }
    updated_keys: set[str] = set()
    added = 0
    updated = 0
    for item in parsed:
        key = project_key(item)
        if not key:
            continue
        index = _find_existing(item, existing, by_title, by_url)
        if index is not None:
            existing[index] = _update_project(existing[index], item)
            updated_keys.add(key)
            updated += 1
        else:
            appended = copy.deepcopy(item)
            title = str(appended.get("title") or appended.get("name") or "").strip()
            appended["title"] = title
            appended["name"] = title
            existing.append(appended)
            by_title[key] = len(existing) - 1
            url = _url_key(appended)
            if url:
                by_url[url] = len(existing) - 1
            updated_keys.add(key)
            added += 1

    preserved_keys = {
        project_key(project)
        for project in existing
        if project_key(project) not in updated_keys
    }
    used_ids = {str(fact.get("id")) for fact in existing_facts if fact.get("id")}
    facts = _project_facts(
        existing,
        existing_facts=existing_facts,
        preserved_keys=preserved_keys,
        used_ids=used_ids,
    )
    preserved = sum(
        1
        for project in existing
        if project_key(project) and project_key(project) not in updated_keys
    )
    return ProjectMergeResult(
        projects=existing,
        project_facts=facts,
        mode=mode,
        added=added,
        updated=updated,
        preserved=preserved,
    )
