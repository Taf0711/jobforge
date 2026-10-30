"""Sequential autopilot for discovery, tailoring, and bounded submission.

Autopilot is deliberately smaller than Jobbie's bulk model:

- It processes one application at a time.
- It only uses ATS adapters that do not require employer account creation.
- It requires the explicit ``autopilot_submit`` risk flag before submitting.
- It stops on captcha, 2FA, account wall, or a non-submitted result.
- It shares the 10/day cap with single-application unattended submit.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import db, quality, risk, services
from .automation.answers import AnswerBank
from .automation.cognition import classify_missing
from .automation.detect import ats_for_url
from .config import Config
from .locking import locked_action
from .paths import base_dir
from .queue import Queue

# Workday is excluded: it normally requires creating an employer account, which
# JobForge does not do. Indeed/LinkedIn are excluded by services.apply_job.
AUTOPILOT_ATS = frozenset({"lever", "greenhouse", "ashby", "workable", "smartrecruiters"})
DIGEST_FILE = "autopilot_last.json"
STATE_FILE = "autopilot_state.json"
TRACE_FILE = "autopilot_trace.jsonl"
MAX_LIMIT = 10


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def read_state(config: Config) -> dict[str, Any] | None:
    path = Path(config.data_dir) / STATE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_state(config: Config, **updates: Any) -> dict[str, Any]:
    state = read_state(config) or {}
    state.update(updates)
    state["updated_at"] = _now()
    _write_private(
        Path(config.data_dir) / STATE_FILE,
        json.dumps(state, indent=2, sort_keys=True) + "\n",
    )
    return state


def trace_event(config: Config, event: str, **fields: Any) -> dict[str, Any]:
    payload = {"ts": _now(), "event": event, **fields}
    path = Path(config.data_dir) / TRACE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return payload


def read_trace(
    config: Config, limit: int = 80, *, offset: int | None = None
) -> list[dict[str, Any]]:
    """Read the event trace.

    With ``offset=None`` (the default) return the newest ``limit`` events for a
    snapshot/UI. With an integer offset return up to ``limit`` events starting at
    that zero-based event index; ``watch`` uses this so a long trace is never
    silently skipped after the newest-N window fills.
    """
    path = Path(config.data_dir) / TRACE_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    cap = max(1, min(500, int(limit)))
    if offset is None:
        return events[-cap:]
    start = max(0, int(offset))
    return events[start : start + cap]


def process_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
    except OSError:
        return False
    return True


def _sync(
    config: Config,
    stats: dict[str, Any],
    stage: str,
    *,
    job: dict[str, Any] | None = None,
    status: str = "running",
) -> None:
    write_state(
        config,
        status=status,
        stage=stage,
        run_id=stats.get("_run_id"),
        pid=os.getpid(),
        started_at=stats.get("started_at", ""),
        limit=stats.get("limit", 0),
        dry_run=bool(stats.get("dry_run")),
        current_job=job,
        counters={
            "prepared": int(stats.get("prepared", 0)),
            "submitted": int(stats.get("submitted", 0)),
            "skipped": int(stats.get("skipped", 0)),
            "failed": int(stats.get("failed", 0)),
        },
        stopped_reason=str(stats.get("stopped_reason", "")),
        finished_at=str(stats.get("finished_at", "")),
        queue=stats.get("queue"),
        digest_path=str(stats.get("digest_path", "")),
    )


def status_payload(config: Config) -> dict[str, Any]:
    state = read_state(config)
    running = False
    if state and str(state.get("status") or "") in {"queued", "running"}:
        running = process_alive(state.get("pid"))
    queue_path = Path(config.data_dir) / "queue.json"
    try:
        queue = Queue(queue_path).summary()
    except Exception:
        queue = {}
    return {
        "state": state,
        "running": running,
        "trace": read_trace(config, limit=120),
        "digest": latest_digest(config),
        "queue": queue,
    }


def ats_for_job(job: Any) -> str:
    return ats_for_url(str(getattr(job, "apply_url", "") or getattr(job, "url", "")))


def supported_job(job: Any) -> bool:
    return ats_for_job(job) in AUTOPILOT_ATS


def _write_digest(config: Config, payload: dict[str, Any]) -> Path:
    path = Path(config.data_dir) / DIGEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def latest_digest(config: Config) -> dict[str, Any] | None:
    path = Path(config.data_dir) / DIGEST_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def launch_subprocess(
    config: Config,
    *,
    limit: int = 5,
    min_score: float | None = None,
    scan: bool = False,
    score: bool = False,
    dry_run: bool = False,
) -> int:
    """Start one autopilot pass in a detached process."""
    cmd = [sys.executable, "-m", "jobforge"]
    if config.config_path:
        cmd.extend(["--config", str(config.config_path)])
    cmd.extend(["autopilot", "run", "--limit", str(max(1, min(MAX_LIMIT, int(limit))))])
    if min_score is not None:
        cmd.extend(["--min-score", str(float(min_score))])
    if scan:
        cmd.append("--scan")
    if score:
        cmd.append("--score")
    if dry_run:
        cmd.append("--dry-run")
    log_path = Path(config.data_dir) / "autopilot.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.touch(exist_ok=True)
    try:
        log_path.chmod(0o600)
    except OSError:
        pass
    with log_path.open("a", encoding="utf-8") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(base_dir()),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    write_state(
        config,
        status="queued",
        stage="starting",
        pid=int(proc.pid),
        started_at=_now(),
        finished_at="",
        run_id=None,
        dry_run=bool(dry_run),
        limit=max(1, min(MAX_LIMIT, int(limit))),
        current_job=None,
        counters={"prepared": 0, "submitted": 0, "skipped": 0, "failed": 0},
        stopped_reason="",
    )
    trace_event(
        config,
        "queued",
        pid=int(proc.pid),
        dry_run=bool(dry_run),
        limit=max(1, min(MAX_LIMIT, int(limit))),
        scan=bool(scan),
        score=bool(score),
    )
    return int(proc.pid)


@locked_action
def run_once(
    config: Config,
    conn: sqlite3.Connection,
    *,
    limit: int = 5,
    min_score: float | None = None,
    scan: bool = False,
    score: bool = False,
    dry_run: bool = False,
    sleep_seconds: float = 0.0,
) -> dict[str, Any]:
    """Run one sequential autopilot pass.

    ``dry_run`` only tailors and prepares; it never opens a browser or reserves
    a submission slot. ``limit`` is capped at 10 and the shared daily cap is
    checked before every submission.
    """
    limit = max(1, min(MAX_LIMIT, int(limit or 1)))
    profile, prefs = services.require_profile(config, conn)
    queue = Queue(Path(config.data_dir) / "queue.json")
    stats: dict[str, Any] = {
        "started_at": _now(),
        "limit": limit,
        "dry_run": bool(dry_run),
        "scanned": 0,
        "queued": 0,
        "prepared": 0,
        "submitted": 0,
        "skipped": 0,
        "failed": 0,
        "stopped_reason": "",
        "ats": sorted(AUTOPILOT_ATS),
    }

    run_id = db.start_run(conn, "autopilot")
    stats["_run_id"] = run_id
    _sync(config, stats, "starting")
    trace_event(
        config,
        "run_started",
        run_id=run_id,
        pid=os.getpid(),
        limit=limit,
        dry_run=bool(dry_run),
    )
    try:
        if scan:
            _sync(config, stats, "scanning")
            trace_event(config, "scan_started", run_id=run_id)
            scan_stats = services.scan(config, conn)
            stats["scanned"] = int(scan_stats.get("new", 0) or 0)
            trace_event(config, "scan_finished", run_id=run_id, new=stats["scanned"])
        if score:
            _sync(config, stats, "scoring")
            trace_event(config, "score_started", run_id=run_id)
            services.rescore(config, conn)
            trace_event(config, "score_finished", run_id=run_id)

        _sync(config, stats, "queueing")
        trace_event(config, "queue_started", run_id=run_id)
        before = len(queue.list_items())
        queue.build(
            conn,
            profile,
            prefs,
            limit=max(limit * 3, limit),
            min_score=prefs.min_match if min_score is None else float(min_score),
        )
        stats["queued"] = max(0, len(queue.list_items()) - before)
        stats["queue"] = queue.summary()
        trace_event(
            config,
            "queue_built",
            run_id=run_id,
            queued=stats["queued"],
            queue=stats["queue"],
        )

        # ``prepared`` and ``submitted`` both count the same job when autopilot
        # prepares and submits it. Bound the loop with an attempt counter so a
        # submitted job consumes one slot, not two.
        attempted = 0
        while attempted < limit:
            item = queue.next_item() if dry_run else queue.next_actionable()
            if item is None:
                break
            job = db.get_job(conn, item.job_id)
            job_info = {
                "job_id": item.job_id,
                "company": item.company,
                "title": item.title,
                "ats": ats_for_job(job) if job else "",
            }
            if job is None:
                queue.mark_failed(item.job_id, "Job not found")
                stats["failed"] += 1
                trace_event(config, "job_failed", run_id=run_id, **job_info, error="Job not found")
                continue
            if not supported_job(job):
                queue.mark_skipped(
                    item.job_id,
                    note=f"autopilot ATS allowlist: {ats_for_job(job) or 'unknown'}",
                )
                stats["skipped"] += 1
                trace_event(
                    config,
                    "job_skipped",
                    run_id=run_id,
                    **job_info,
                    reason="ATS allowlist",
                )
                _sync(config, stats, "queueing")
                continue

            if not dry_run:
                allowed, reason, _policy = risk.can_submit_autopilot(config)
                if not allowed:
                    stats["stopped_reason"] = reason
                    trace_event(config, "stopped", run_id=run_id, reason=reason)
                    break
            else:
                stats["stopped_reason"] = "dry_run"

            attempted += 1
            _sync(
                config,
                stats,
                "applying" if item.status == "prepared" else "tailoring",
                job=job_info,
            )
            trace_event(
                config,
                "job_started",
                run_id=run_id,
                status=item.status,
                **job_info,
            )

            if item.status == "prepared":
                pdf = str(item.resume_path or "").strip()
                if not pdf:
                    queue.mark_failed(item.job_id, "Prepared item has no resume path")
                    stats["failed"] += 1
                    continue
            else:
                try:
                    tailored = services.tailor_job(
                        config, conn, item.job_id, profile_id=item.profile_id
                    )
                    pdf = str(tailored.get("pdf") or "").strip()
                    if not pdf:
                        raise ValueError("tailor_job did not return a PDF")
                    trace_event(
                        config,
                        "tailored",
                        run_id=run_id,
                        **job_info,
                        pdf=pdf,
                        llm_used=bool(tailored.get("llm_used")),
                        warnings=list(tailored.get("warnings") or [])[:10],
                    )
                except quality.QualityBlocked as exc:
                    queue.mark_skipped(item.job_id, note=f"quality blockers: {exc}")
                    stats["skipped"] += 1
                    trace_event(
                        config, "job_skipped", run_id=run_id, **job_info, reason=str(exc)[:300]
                    )
                    continue
                except Exception as exc:  # noqa: BLE001 - keep the loop sequential
                    queue.mark_failed(item.job_id, str(exc)[:500])
                    stats["failed"] += 1
                    trace_event(
                        config, "job_failed", run_id=run_id, **job_info, error=str(exc)[:300]
                    )
                    continue

                queue.mark_prepared(item.job_id, pdf)
                stats["prepared"] += 1
                trace_event(config, "prepared", run_id=run_id, **job_info, pdf=pdf)
                if dry_run:
                    continue

            _sync(config, stats, "applying", job=job_info)
            trace_event(config, "apply_started", run_id=run_id, **job_info)
            try:
                report = services.apply_job(
                    config,
                    conn,
                    item.job_id,
                    profile_id=item.profile_id,
                    allow_submit=True,  # security-audit: allow auto-submit enabled
                    submission_mode="autopilot",
                    hold_seconds=0,
                )
            except risk.RiskError as exc:
                stats["stopped_reason"] = str(exc)
                trace_event(config, "stopped", run_id=run_id, reason=str(exc)[:300])
                break
            except quality.QualityBlocked as exc:
                queue.mark_skipped(item.job_id, note=f"quality blockers: {exc}")
                stats["skipped"] += 1
                trace_event(
                    config, "job_skipped", run_id=run_id, **job_info, reason=str(exc)[:300]
                )
                continue
            except Exception as exc:  # noqa: BLE001
                queue.mark_failed(item.job_id, str(exc)[:500])
                stats["failed"] += 1
                trace_event(
                    config, "job_failed", run_id=run_id, **job_info, error=str(exc)[:300]
                )
                continue

            if report.get("submitted") is True:
                queue.mark_submitted(item.job_id)
                stats["submitted"] += 1
                trace_event(config, "submitted", run_id=run_id, **job_info)
            elif report.get("captcha"):
                queue.mark_prepared(item.job_id, pdf)
                stats["stopped_reason"] = "captcha"
                trace_event(
                    config, "blocked", run_id=run_id, **job_info, reason="captcha"
                )
                break
            elif report.get("required_missing"):
                missing = [str(entry) for entry in report.get("required_missing") or []]
                item_profile, item_prefs = services.require_profile_by_id(
                    conn, item.profile_id
                )
                review = classify_missing(
                    missing,
                    item_profile,
                    item_prefs,
                    AnswerBank(Path(config.data_dir) / "answers.json"),
                )
                queue.mark_prepared(
                    item.job_id,
                    pdf,
                    note="required fields: " + "; ".join(missing)[:400],
                )
                stats["stopped_reason"] = "required_fields"
                stats["required_missing"] = missing
                stats["required_review"] = review
                trace_event(
                    config,
                    "blocked",
                    run_id=run_id,
                    **job_info,
                    reason="required_fields",
                    required_missing=missing,
                    required_review=review,
                )
                break
            else:
                queue.mark_prepared(item.job_id, pdf)
                stats["stopped_reason"] = "not_submitted"
                notes = report.get("notes") or []
                reason = str(notes[-1]) if notes else "not submitted"
                trace_event(
                    config,
                    "blocked",
                    run_id=run_id,
                    **job_info,
                    reason=reason[:300],
                )
                break
            _sync(config, stats, "queueing", job=job_info)
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)

        stats["finished_at"] = _now()
        stats["queue"] = queue.summary()
        stats["digest_path"] = str(_write_digest(config, stats))
        trace_event(
            config,
            "run_finished",
            run_id=run_id,
            status="ok",
            submitted=stats["submitted"],
            prepared=stats["prepared"],
            stopped_reason=stats["stopped_reason"],
            queue=stats["queue"],
        )
        _sync(config, stats, "done", status="ok")
        db.finish_run(conn, run_id, "ok", stats)
        return stats
    except Exception as exc:
        stats["finished_at"] = _now()
        stats["error"] = str(exc)[:500]
        trace_event(config, "run_failed", run_id=run_id, error=stats["error"])
        _sync(config, stats, "failed", status="failed")
        db.finish_run(conn, run_id, "failed", stats)
        raise
