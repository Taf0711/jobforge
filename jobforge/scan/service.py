"""Scan orchestration: fetch, dedupe, filter, quick-score, persist."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .. import db
from ..config import Config
from ..models import Job, Preferences, Profile
from ..sources.direct import fetch_job_url
from ..sources.registry import FetchReport, fetch_configured
from .quick import company_key, filter_job, quick_scan, title_key
from .registry import fetch_registry, registry_path

_COMPETITION_RANK = {
    "cleared": 4,
    "public_sector": 3,
    "direct_ats": 2,
    "community": 1,
    "mass_aggregator": 0,
}


def dedupe_jobs(jobs: Iterable[Job]) -> tuple[list[Job], int]:
    """Keep one row per company+title, preferring low-competition sources.

    A direct ATS row beats an aggregator row for the same role because it has the
    canonical employer URL and, usually, the real description. A fresher row
    wins within the same competition profile.
    """
    original = list(jobs)
    best: dict[tuple[str, str], Job] = {}
    unkeyed: list[Job] = []
    for job in original:
        key = (company_key(job.company), title_key(job.title))
        if not all(key):
            unkeyed.append(job)
            continue
        current = best.get(key)
        if current is None:
            best[key] = job
            continue
        current_rank = _COMPETITION_RANK.get(current.competition_profile, 0)
        job_rank = _COMPETITION_RANK.get(job.competition_profile, 0)
        if job_rank > current_rank:
            best[key] = job
            continue
        if job_rank == current_rank:
            current_desc = len(current.description or "")
            job_desc = len(job.description or "")
            if (job_desc, job.posted_at or "") > (current_desc, current.posted_at or ""):
                best[key] = job
    selected = list(best.values()) + unkeyed
    return selected, max(0, len(original) - len(selected))


def scan_jobs(
    config: Config,
    conn: sqlite3.Connection,
    profile: Profile,
    prefs: Preferences,
    *,
    only: list[str] | None = None,
    urls: list[str] | None = None,
    apply_filters: bool = True,
    quick: bool = True,
) -> dict[str, Any]:
    """Run one scan. The caller owns the run row/transaction boundary."""
    stats: dict[str, Any] = {
        "new": 0,
        "seen": 0,
        "filtered": 0,
        "filtered_reasons": {},
        "deduped": 0,
        "deferred": 0,
        "scanned": 0,
        "errors": [],
        "per_source": {},
        "registry": "",
    }
    if isinstance(only, str):
        only = [only]
    jobs: list[Job] = []
    if urls:
        for url in urls:
            try:
                jobs.append(fetch_job_url(url))
            except Exception as exc:  # one URL must not abort the scan
                stats["errors"].append(f"{url}: {exc}")
    else:
        path = registry_path(config)
        if path is not None:
            stats["registry"] = str(path)
            report = fetch_registry(config, only=only)
        else:
            report = fetch_configured(config, only=only)
        jobs = report.jobs
        stats["errors"] = list(report.errors)
        stats["per_source"] = dict(report.per_source)

    jobs, deduped = dedupe_jobs(jobs)
    stats["deduped"] = deduped

    if quick:
        from .. import services as services_module

        archetypes = services_module.load_archetypes(config)
    else:
        archetypes = {}

    applications = db.list_applications(conn, int(profile.id or 0), limit=500)
    scan_cfg = dict(config.scan or {})
    max_new = 0 if urls else int(scan_cfg.get("max_new") or 0)

    for job in jobs:
        if apply_filters and not urls:
            keep, reason = filter_job(
                job,
                prefs=prefs,
                applications=applications,
                scan_cfg=scan_cfg,
            )
            if not keep:
                stats["filtered"] += 1
                key = reason or "filtered"
                stats["filtered_reasons"][key] = stats["filtered_reasons"].get(key, 0) + 1
                existing = conn.execute(
                    "SELECT id FROM jobs WHERE fingerprint=?", (job.fingerprint,)
                ).fetchone()
                if existing is not None:
                    db.delete_scan(conn, int(existing["id"]), int(profile.id or 0))
                continue
        before = conn.execute(
            "SELECT id FROM jobs WHERE fingerprint=?", (job.fingerprint,)
        ).fetchone()
        if before is None and max_new > 0 and stats["new"] >= max_new:
            stats["deferred"] += 1
            continue
        job_id = db.upsert_job(conn, job)
        job.id = job_id
        stats["new" if before is None else "seen"] += 1
        if quick:
            result = quick_scan(
                job,
                profile=profile,
                prefs=prefs,
                archetypes=archetypes,
                scan_cfg=scan_cfg,
            )
            db.set_scan(conn, result)
            stats["scanned"] += 1
    return stats
