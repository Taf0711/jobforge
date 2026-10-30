"""Service layer shared by CLI and web app."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import db, quality
from .config import Config
from .matching.resume import load_profile_file, merge_profiles, profile_from_resume
from .matching.score import score_all
from .models import (
    Application,
    ApplicationStatus,
    Job,
    Preferences,
    Profile,
    RunStatus,
    slugify,
    utcnow,
)
from .locking import locked_action
from .paths import base_dir
from .sources.base import SourceError
from .sources.direct import fetch_job_url
from .sources.registry import fetch_configured
from .tailoring.llm import LLMClient
from .tailoring.tailor import tailor_resume


def _seed_preferences(config: Config) -> Preferences:
    raw = dict(config.preferences or {})
    # Accept both snake_case (our schema) and the older camelCase keys.
    mapping = {
        "titles": "titles",
        "industries": "industries",
        "locations": "locations",
        "workModes": "work_modes",
        "work_modes": "work_modes",
        "employmentTypes": "employment_types",
        "employment_types": "employment_types",
        "experienceLevels": "experience_levels",
        "experience_levels": "experience_levels",
        "yearsOfExperience": "years_of_experience",
        "years_of_experience": "years_of_experience",
        "salaryMin": "salary_min",
        "salary_min": "salary_min",
        "salaryMax": "salary_max",
        "salary_max": "salary_max",
        "needsSponsorship": "needs_sponsorship",
        "needs_sponsorship": "needs_sponsorship",
        "openToRelocation": "open_to_relocation",
        "open_to_relocation": "open_to_relocation",
        "securityClearance": "security_clearance",
        "security_clearance": "security_clearance",
        "excludedCompanies": "excluded_companies",
        "excluded_companies": "excluded_companies",
        "excludedKeywords": "excluded_keywords",
        "excluded_keywords": "excluded_keywords",
        "includedKeywords": "included_keywords",
        "included_keywords": "included_keywords",
        "minMatch": "min_match",
        "min_match": "min_match",
        "oneApplicationPerCompany": "one_application_per_company",
        "one_application_per_company": "one_application_per_company",
    }
    data: dict[str, Any] = {}
    for key, value in raw.items():
        if key in mapping:
            data[mapping[key]] = value
    return Preferences.from_dict(data)


class SetupRequired(RuntimeError):
    """A read-only query was made before a human initialized the workbench."""


def read_profile(
    config: Config, conn: sqlite3.Connection
) -> tuple[Profile | None, Preferences | None]:
    """Read the active profile without creating or repairing any rows.

    Read-only callers must use this instead of ``ensure_profile``. A missing
    profile returns ``(None, None)``; missing preferences return defaults
    without persisting them.
    """
    profile = db.get_active_profile(conn)
    if profile is None:
        return None, None
    prefs = db.get_preferences(conn, profile.id or 0)
    return profile, prefs or Preferences()


def require_profile(
    config: Config, conn: sqlite3.Connection
) -> tuple[Profile, Preferences]:
    """Return the active profile or raise ``SetupRequired``."""
    profile, prefs = read_profile(config, conn)
    if profile is None or prefs is None:
        raise SetupRequired("JobForge setup is incomplete; run `jobforge setup` first")
    return profile, prefs


def require_profile_by_id(
    conn: sqlite3.Connection, profile_id: int
) -> tuple[Profile, Preferences]:
    """Resolve an explicit profile for a queued or approved action.

    Execution paths must use the profile recorded when the work was approved,
    not whichever profile happens to be active later. This helper never changes
    the active profile.
    """
    profile = db.get_profile(conn, profile_id)
    if profile is None:
        raise SetupRequired(
            "The profile for this approved action no longer exists; recreate it or deny the action"
        )
    prefs = db.get_preferences(conn, profile.id or 0)
    return profile, prefs


def ensure_profile(config: Config, conn: sqlite3.Connection) -> tuple[Profile, Preferences]:
    profile = db.get_active_profile(conn)
    if profile is None:
        if config.profile_path and Path(config.profile_path).exists():
            profile = load_profile_file(config.profile_path)
        elif config.resume_path and Path(config.resume_path).exists():
            profile = profile_from_resume(config.resume_path)
        else:
            profile = Profile(name="default")
        profile.id = db.save_profile(conn, profile)
        db.set_active_profile(conn, profile.id)
        db.save_preferences(conn, profile.id, _seed_preferences(config))
    else:
        # Fill missing preferences from config on first web/CLI run.
        row = conn.execute("SELECT 1 FROM preferences WHERE profile_id=?", (profile.id,)).fetchone()
        if not row:
            db.save_preferences(conn, profile.id or 0, _seed_preferences(config))
    prefs = db.get_preferences(conn, profile.id or 0)
    return profile, prefs


@locked_action
def refresh_profile_from_resume(config: Config, conn: sqlite3.Connection, path: str | Path) -> Profile:
    profile, _ = ensure_profile(config, conn)
    parsed = profile_from_resume(path)
    merged = merge_profiles(profile, parsed)
    merged.id = profile.id
    db.save_profile(conn, merged)
    db.set_active_profile(conn, merged.id or 0)
    db.log_event(conn, None, "profile_updated", f"resume: {path}")
    return merged


@locked_action
def scan(
    config: Config,
    conn: sqlite3.Connection,
    *,
    only: list[str] | None = None,
    urls: list[str] | None = None,
) -> dict[str, Any]:
    run_id = db.start_run(conn, "scan")
    stats: dict[str, Any] = {
        "new": 0,
        "seen": 0,
        "filtered": 0,
        "deferred": 0,
        "errors": [],
        "per_source": {},
    }
    try:
        jobs: list[Job] = []
        if urls:
            for url in urls:
                try:
                    jobs.append(fetch_job_url(url))
                except Exception as exc:
                    stats["errors"].append(f"{url}: {exc}")
        else:
            report = fetch_configured(config, only=only)
            jobs = report.jobs
            stats["errors"] = report.errors
            stats["per_source"] = report.per_source
            scan_cfg = config.scan or {}
            include = [
                str(k).strip().lower()
                for k in (scan_cfg.get("keywords") or [])
                if str(k).strip()
            ]
            exclude = [
                str(k).strip().lower()
                for k in (scan_cfg.get("exclude_keywords") or [])
                if str(k).strip()
            ]
            if include or exclude:
                before_filter = len(jobs)
                jobs = [
                    job
                    for job in jobs
                    if (not include or any(k in (job.title or "").lower() for k in include))
                    and not any(k in (job.title or "").lower() for k in exclude)
                ]
                stats["filtered"] = before_filter - len(jobs)
            # Freshest first; the cap keeps the first web scan from turning
            # into a multi-minute deep-score request.
            jobs.sort(
                key=lambda job: ((job.posted_at or ""), (job.title or "").lower()),
                reverse=True,
            )
        max_new = 0 if urls else int((config.scan or {}).get("max_new") or 0)
        for job in jobs:
            before = conn.execute("SELECT id FROM jobs WHERE fingerprint=?", (job.fingerprint,)).fetchone()
            if before is None and max_new > 0 and stats["new"] >= max_new:
                stats["deferred"] += 1
                continue
            db.upsert_job(conn, job)
            stats["new" if before is None else "seen"] += 1
        status = RunStatus.OK
    except Exception as exc:
        stats["errors"].append(str(exc))
        status = RunStatus.FAILED
    db.finish_run(conn, run_id, status.value, stats)
    return stats


@locked_action
def rescore(
    config: Config, conn: sqlite3.Connection, *, only_unscored: bool = False
) -> dict[str, Any]:
    profile, prefs = require_profile(config, conn)
    run_id = db.start_run(conn, "score")
    try:
        result = score_all(
            conn,
            profile,
            prefs,
            only_unscored=only_unscored,
            scoring_v2=config.features.scoring_v2,
        )
        db.finish_run(conn, run_id, RunStatus.OK.value, result)
        return result
    except Exception as exc:
        db.finish_run(conn, run_id, RunStatus.FAILED.value, {"error": str(exc)})
        raise


_PROTECTED_APPLICATION_STATUSES = frozenset(
    {
        ApplicationStatus.SUBMITTED.value,
        ApplicationStatus.INTERVIEW.value,
        ApplicationStatus.OFFER.value,
        ApplicationStatus.REJECTED.value,
        ApplicationStatus.WITHDRAWN.value,
    }
)
_PROTECTED_EVENT_KINDS = ("applied", "submitted", "unattended_submit", "autopilot_submit")


def _protected_application_reason(
    conn: sqlite3.Connection, existing: sqlite3.Row | None
) -> str:
    """Return a refusal reason when an application has a prior submission.

    A protected row must not reach an LLM call or the renderer: output paths are
    stable, so a late refusal can still replace the bytes that were sent.
    """
    if existing is None:
        return ""
    status = str(existing["status"] or "")
    if status in _PROTECTED_APPLICATION_STATUSES:
        return f"Application is already {status}"
    if existing["submitted_at"]:
        return "Application has a submitted_at timestamp"
    placeholders = ",".join("?" for _ in _PROTECTED_EVENT_KINDS)
    prior = conn.execute(
        f"SELECT 1 FROM events WHERE application_id=? AND kind IN ({placeholders}) LIMIT 1",
        (int(existing["id"]), *_PROTECTED_EVENT_KINDS),
    ).fetchone()
    if prior is not None:
        return "Application has a prior submission event"
    return ""


@locked_action
def tailor_job(
    config: Config,
    conn: sqlite3.Connection,
    job_id: int,
    *,
    profile_id: int | None = None,
) -> dict[str, Any]:
    job = db.get_job(conn, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    if profile_id is None:
        profile, prefs = require_profile(config, conn)
    else:
        profile, prefs = require_profile_by_id(conn, profile_id)
    resume_path = profile.resume_path or (str(config.resume_path) if config.resume_path else "")
    if not resume_path or not Path(resume_path).exists():
        raise ValueError("No base resume found. Set profile.resume_path or config.resume.")
    profile.resume_path = resume_path

    existing = conn.execute(
        "SELECT * FROM applications WHERE job_id=? AND profile_id=?",
        (job.id, profile.id),
    ).fetchone()
    protected_reason = _protected_application_reason(conn, existing)
    if protected_reason:
        raise ValueError(
            f"{protected_reason}; refusing to replace the resume that was sent. "
            "Tailor a new application only after reopening it."
        )

    llm = LLMClient(config)
    run_id = db.start_run(conn, "tailor")
    try:
        result = tailor_resume(
            profile,
            job,
            config.output_dir,
            llm=llm,
            archetypes=load_archetypes(config),
            mode=config.tailoring.mode,
        )
        if not result.ats.get("ok"):
            errors = "; ".join(result.ats.get("errors") or ["unknown ATS failure"])
            raise ValueError(f"Tailored PDF failed ATS checks: {errors}")

        if existing:
            app_id = int(existing["id"])
            db.update_application(
                conn,
                app_id,
                ats=job.source,
                resume_path=result.pdf_path,
                cover_letter_path=result.cover_letter_path,
                notes="Tailored resume regenerated",
            )
            db.log_event(conn, app_id, "tailored", result.pdf_path)
        else:
            app = Application(
                job_id=job.id,
                profile_id=profile.id,
                status=ApplicationStatus.DRAFT.value,
                ats=job.source,
                resume_path=result.pdf_path,
                cover_letter_path=result.cover_letter_path,
                answers={},
                notes="Tailored resume generated",
            )
            app_id = db.create_application(conn, app)
            db.log_event(conn, app_id, "tailored", result.pdf_path)
        stats = {
            "application_id": app_id,
            "pdf": result.pdf_path,
            "cover_letter": result.cover_letter_path,
            "ats": result.ats,
            "facts": len(result.selected_fact_ids),
            "warnings": result.warnings,
            "llm_used": result.llm_used,
            "llm_model": result.llm_model,
            "mode": result.mode,
            "reused_application": bool(existing),
        }
        db.finish_run(conn, run_id, RunStatus.OK.value, stats)
        return stats
    except Exception as exc:
        db.finish_run(conn, run_id, RunStatus.FAILED.value, {"error": str(exc)})
        raise


@locked_action
def apply_job(
    config: Config,
    conn: sqlite3.Connection,
    job_id: int,
    *,
    allow_submit: bool = False,
    hold_seconds: int = 0,
    resume_path: str | None = None,
    submission_mode: str = "unattended",
    review_only: bool = False,
    profile_id: int | None = None,
) -> dict[str, Any]:
    from .automation.base import run_application

    job = db.get_job(conn, job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found")
    if profile_id is None:
        profile, prefs = require_profile(config, conn)
    else:
        profile, prefs = require_profile_by_id(conn, profile_id)
    existing = conn.execute(
        "SELECT * FROM applications WHERE job_id=? AND profile_id=?", (job_id, profile.id)
    ).fetchone()
    resume = resume_path or (existing["resume_path"] if existing else "") or profile.resume_path or str(config.resume_path or "")
    if not resume or not Path(resume).exists():
        raise ValueError("No resume PDF found. Run `jobforge tailor --job <id>` first or set a base resume.")

    from .automation.base import is_manual_only_url

    if is_manual_only_url(job.apply_url or job.url):
        raise ValueError(
            "Indeed and LinkedIn are manual-only in JobForge. Open the posting and apply yourself."
        )

    # Defense in depth: CLI, web, and queue already gate, but the service is a
    # public entry point and must not open a browser for a blocked job.
    gate = quality.evaluate(conn, job, profile, prefs, require_prepared=False)
    if not gate.ready:
        raise quality.QualityBlocked(gate)

    if review_only and allow_submit:
        raise ValueError("review_only cannot be combined with allow_submit")
    if review_only and submission_mode == "autopilot":
        raise ValueError("review_only is not valid for autopilot submission")
    auto_submit = False if review_only else bool(allow_submit or config.browser.allow_submit)
    if auto_submit:
        from .risk import RiskError, reserve_autopilot_submission, reserve_submission

        reserve = (
            reserve_autopilot_submission
            if submission_mode == "autopilot"
            else reserve_submission
        )
        allowed, reason, _policy = reserve(config)
        if not allowed:
            raise RiskError(reason)

    run_id = db.start_run(conn, "apply")
    try:
        from .automation.decision_shadow import shadow_enabled, shadow_path

        decision_shadow = shadow_path(config) if shadow_enabled() else None
        cover_letter = str(existing["cover_letter_path"] or "") if existing else ""
        report = run_application(
            job,
            profile,
            prefs,
            config.browser,
            resume,
            cover_letter_path=cover_letter or None,
            answer_bank_path=config.data_dir / "answers.json",
            allow_submit=auto_submit,
            submission_authorized=auto_submit,
            review_only=review_only,
            hold_seconds=hold_seconds,
            non_interactive=submission_mode == "autopilot",
            screenshot_dir=config.data_dir / "screenshots",
            decision_shadow_path=decision_shadow,
            decision_run_id=run_id,
            decision_job_id=job.id,
            decision_application_id=str(existing["id"]) if existing else "",
        )
        status = ApplicationStatus.SUBMITTED.value if report.submitted else ApplicationStatus.PREPARED.value
        if report.captcha and not report.submitted:
            status = ApplicationStatus.PREPARED.value
        answers = {f.label: f.value for f in report.fields}
        app = Application(
            job_id=job.id,
            profile_id=profile.id,
            status=status,
            ats=report.ats,
            resume_path=resume,
            cover_letter_path=cover_letter,
            answers=answers,
            screenshot_path=report.screenshot,
            error_code="captcha" if report.captcha else "",
            notes="; ".join(report.notes),
            submitted_at=utcnow() if report.submitted else None,
        )
        app_id = db.create_application(conn, app)
        db.log_event(conn, app_id, "applied" if report.submitted else "prepared", report.url)
        if auto_submit and report.submitted:
            event = "autopilot_submit" if submission_mode == "autopilot" else "unattended_submit"
            db.log_event(conn, app_id, event, "risk-consented submission")
        stats = {"application_id": app_id, **report.to_dict()}
        db.finish_run(conn, run_id, RunStatus.OK.value, {"application_id": app_id, "submitted": report.submitted})
        return stats
    except Exception as exc:
        db.finish_run(conn, run_id, RunStatus.FAILED.value, {"error": str(exc)})
        raise


def launch_apply_subprocess(
    config: Config,
    job_id: int,
    hold_seconds: int = 600,
    *,
    review_only: bool = False,
) -> int:
    """Start the headed apply flow in a separate process (used by the web UI)."""
    cmd = [sys.executable, "-m", "jobforge"]
    if config.config_path:
        cmd.extend(["--config", str(config.config_path)])
    cmd.extend(["apply", "--job", str(job_id), "--hold", str(hold_seconds)])
    if review_only:
        cmd.append("--review-only")
    proc = subprocess.Popen(
        cmd,
        cwd=str(base_dir()),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return int(proc.pid)


def export_data(config: Config, conn: sqlite3.Connection) -> Path:
    path = config.data_dir / f"jobforge-export-{utcnow().replace(':', '-')}.json"
    path.write_text(json.dumps(db.export_all(conn), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_network(config: Config | None) -> Any | None:
    """Load the local referral network, if a network file exists."""
    if config is None:
        return None
    from .networking import NetworkGraph

    candidates = [
        Path(config.data_dir) / "network.yml",
        Path(config.data_dir) / "network.yaml",
        base_dir() / "network.yml",
        base_dir() / "network.yaml",
    ]
    network_path = next((path for path in candidates if path.exists()), None)
    return NetworkGraph(network_path) if network_path is not None else None


def load_archetypes(config: Config | None) -> dict[str, Any]:
    """Load the local role-archetype file, if the user imported one."""
    if config is None:
        return {}
    from .matching.archetypes import load_archetypes as _load

    candidates = [
        Path(config.data_dir) / "role_archetypes.yml",
        Path(config.data_dir) / "role_archetypes.yaml",
        base_dir() / "role_archetypes.yml",
        base_dir() / "role_archetypes.yaml",
    ]
    path = next((item for item in candidates if item.exists()), None)
    return _load(path) if path is not None else {}


def warm_summary(graph: Any, company: str, fit: float) -> dict[str, Any]:
    """Build the display-only warm-path summary for one job."""
    from .networking import channel, modify_fit

    path = graph.warm_path(company)
    adjusted, delta, adjust_reason = modify_fit(fit, path)
    return {
        "company": path.company,
        "score": path.score,
        "contacts": path.contacts,
        "reason": path.reason,
        "action": path.action,
        "channel": channel(fit, path),
        "fit": fit,
        "adjusted_fit": adjusted,
        "delta": delta,
        "adjust_reason": adjust_reason,
        "warnings": graph.warnings,
    }


def job_context(
    conn: sqlite3.Connection,
    job_id: int,
    profile_id: int,
    config: Config | None = None,
) -> dict[str, Any] | None:
    job = db.get_job(conn, job_id)
    if not job:
        return None
    match = db.get_match(conn, job_id, profile_id)
    app = conn.execute(
        "SELECT * FROM applications WHERE job_id=? AND profile_id=?", (job_id, profile_id)
    ).fetchone()

    fit = float(match.score) if match else 0.0
    graph = load_network(config)
    warm = warm_summary(graph, job.company, fit) if graph else None

    application = dict(app) if app else None
    if application is not None:
        application["answers"] = json.loads(application.get("answers_json") or "{}")
    return {
        "job": asdict(job),
        "match": asdict(match) if match else None,
        "application": application,
        "warm": warm,
    }
