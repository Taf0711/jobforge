"""Supervised application queue.

The queue ranks scored jobs, prepares tailored resumes, and opens applications
one at a time. It records only what a human or the review-mode service layer
reports; it never infers that a submission happened.
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from . import db, quality, services
from .config import Config
from .models import Preferences, Profile, utcnow

QUEUE_STATUSES = ("pending", "prepared", "submitted", "skipped", "failed")
VALID_STATUSES = frozenset(QUEUE_STATUSES)
QUEUE_VERSION = 1

_UNSET = object()


@dataclass
class QueueItem:
    job_id: int
    profile_id: int
    status: str = "pending"
    score: float = 0.0
    company: str = ""
    title: str = ""
    resume_path: str = ""
    error: str = ""
    created_at: str = field(default_factory=utcnow)
    updated_at: str = field(default_factory=utcnow)
    note: str = ""

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUSES:
            raise ValueError(f"Invalid queue status: {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "profile_id": self.profile_id,
            "status": self.status,
            "score": self.score,
            "company": self.company,
            "title": self.title,
            "resume_path": self.resume_path,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QueueItem":
        return cls(
            job_id=int(data["job_id"]),
            profile_id=int(data["profile_id"]),
            status=str(data.get("status", "pending")),
            score=float(data.get("score") or 0.0),
            company=str(data.get("company") or ""),
            title=str(data.get("title") or ""),
            resume_path=str(data.get("resume_path") or ""),
            error=str(data.get("error") or ""),
            created_at=str(data.get("created_at") or utcnow()),
            updated_at=str(data.get("updated_at") or utcnow()),
            note=str(data.get("note") or ""),
        )


class Queue:
    """A JSON-backed, ranked queue of application candidates."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._items: list[QueueItem] = []
        self._load()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def list_items(self, status: str | None = None) -> list[QueueItem]:
        if status is None:
            return list(self._items)
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid queue status: {status!r}")
        return [item for item in self._items if item.status == status]

    def next_item(self) -> QueueItem | None:
        """Return the first item still waiting to be prepared."""
        return self._first("pending")

    def next_actionable(self) -> QueueItem | None:
        """Return the next human action: prepared first, then pending."""
        return self._first("prepared") or self._first("pending")

    def summary(self) -> dict[str, int]:
        counts = {status: 0 for status in QUEUE_STATUSES}
        for item in self._items:
            counts[item.status] += 1
        return {"total": len(self._items), **counts}

    # ------------------------------------------------------------------
    # Building
    # ------------------------------------------------------------------

    def build(
        self,
        conn: sqlite3.Connection,
        profile: Profile,
        prefs: Preferences,
        limit: int = 10,
        min_score: float | None = None,
    ) -> list[QueueItem]:
        """Append ranked, scored jobs to the queue and return the new items.

        Jobs already in the queue and jobs with an application row are skipped.
        When the preference is enabled, at most one job per company is queued.
        ``min_score=None`` leaves the threshold unset; pass ``prefs.min_match``
        when the user's minimum should gate the build.
        """
        if limit <= 0:
            return []
        if profile.id is None:
            raise ValueError("Cannot build a queue for an unsaved profile")

        one_per_company = bool(prefs.one_application_per_company)
        existing_ids = {item.job_id for item in self._items}
        existing_companies = {
            item.company.strip().lower() for item in self._items if item.company.strip()
        }
        added: list[QueueItem] = []
        offset = 0
        page_size = max(limit * 2, 20)

        while len(added) < limit:
            rows = db.list_jobs(
                conn,
                profile_id=profile.id,
                min_score=min_score,
                one_per_company=one_per_company,
                limit=page_size,
                offset=offset,
            )
            if not rows:
                break
            for row in rows:
                if len(added) >= limit:
                    break
                job_id = int(row["id"])
                if job_id in existing_ids:
                    continue
                if row.get("application_id") is not None:
                    continue
                company = str(row.get("company") or "")
                company_key = company.strip().lower()
                if one_per_company and company_key and company_key in existing_companies:
                    continue
                item = QueueItem(
                    job_id=job_id,
                    profile_id=profile.id,
                    status="pending",
                    score=float(row.get("score") or 0.0),
                    company=company,
                    title=str(row.get("title") or ""),
                )
                self._items.append(item)
                added.append(item)
                existing_ids.add(job_id)
                if company_key:
                    existing_companies.add(company_key)
            if len(rows) < page_size:
                break
            offset += page_size

        if added:
            self._save()
        return added

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def mark_prepared(self, job_id: int, resume_path: str) -> QueueItem:
        return self._transition(
            job_id,
            "prepared",
            resume_path=str(resume_path or ""),
            error="",
            note="",
        )

    def mark_submitted(self, job_id: int) -> QueueItem:
        return self._transition(job_id, "submitted", error="", note="")

    def mark_skipped(self, job_id: int) -> QueueItem:
        return self._transition(job_id, "skipped", error="", note="")

    def mark_failed(self, job_id: int, error: str) -> QueueItem:
        return self._transition(
            job_id,
            "failed",
            error=str(error) if error is not None else "",
            note="",
        )

    # ------------------------------------------------------------------
    # Supervised actions
    # ------------------------------------------------------------------

    def prepare_next(self, config: Config, conn: sqlite3.Connection) -> QueueItem:
        """Tailor the first pending item and mark it prepared.

        Failures are recorded on the item as ``failed`` and returned; they are
        not reported as a prepared resume.
        """
        item = self._first("pending")
        if item is None:
            raise LookupError("No pending queue items to prepare")
        try:
            result = services.tailor_job(config, conn, item.job_id)
            pdf_path = str(result.get("pdf") or "").strip() if isinstance(result, dict) else ""
            if not pdf_path:
                raise ValueError("tailor_job did not return a PDF path")
        except Exception as exc:
            return self.mark_failed(item.job_id, str(exc))
        prepared = self.mark_prepared(item.job_id, pdf_path)

        # Tailoring created the application row, so the full quality report is
        # meaningful now. Keep the item prepared either way; apply_next blocks
        # before opening a browser if a blocker remains.
        job = db.get_job(conn, item.job_id)
        if job is not None:
            profile, prefs = services.ensure_profile(config, conn)
            report = quality.evaluate(conn, job, profile, prefs)
            if not report.ready:
                blocked = "; ".join(
                    f"{check.name}: {check.detail}" for check in quality.blocking_checks(report)
                )
                return self._transition(
                    item.job_id, "prepared", note=f"quality blockers: {blocked}"
                )
        return prepared

    def apply_next(
        self,
        config: Config,
        conn: sqlite3.Connection,
        *,
        hold_seconds: int = 0,
        confirm: Callable[[], bool] | None = None,
        auto_submit: bool = False,
    ) -> QueueItem:
        """Open the first prepared item's application for human review.

        By default the service layer runs in review mode even if
        ``browser.allow_submit`` is set. ``auto_submit=True`` is the explicit
        risk-consented path: it requires the persisted risk policy and daily
        cap, and still stops on a captcha. A submission is otherwise recorded
        only when the service reports it or a human confirms it via ``confirm``.
        """
        item = self._first("prepared")
        if item is None:
            raise LookupError("No prepared queue items to apply")

        job = db.get_job(conn, item.job_id)
        if job is None:
            return self.mark_failed(item.job_id, "Job not found")
        profile, prefs = services.ensure_profile(config, conn)
        report = quality.evaluate(conn, job, profile, prefs, require_prepared=False)
        if not report.ready:
            raise quality.QualityBlocked(report)

        if auto_submit:
            from .risk import RiskError, can_submit_unattended

            allowed, reason, _policy = can_submit_unattended(config)
            if not allowed:
                raise RiskError(reason)
            run_config = config
        else:
            run_config = _review_mode_config(config)

        kwargs: dict[str, Any] = {"hold_seconds": hold_seconds}
        if auto_submit:
            kwargs["allow_submit"] = True  # security-audit: allow auto-submit enabled
        try:
            report = services.apply_job(run_config, conn, item.job_id, **kwargs)
        except Exception as exc:
            return self.mark_failed(item.job_id, str(exc))

        if not isinstance(report, dict):
            return self.mark_failed(item.job_id, "apply_job returned an invalid report")

        submitted = report.get("submitted")
        if submitted is not None and not isinstance(submitted, bool):
            return self.mark_failed(
                item.job_id, f"apply_job returned a malformed submitted value: {submitted!r}"
            )
        if submitted is True:
            return self._transition(
                item.job_id,
                "submitted",
                error="",
                note="submission reported by apply_job",
            )

        if callable(confirm):
            try:
                confirmed = bool(confirm())
            except Exception as exc:
                return self._transition(
                    item.job_id,
                    "prepared",
                    note=f"confirmation prompt failed: {exc}",
                )
            if confirmed:
                return self._transition(
                    item.job_id,
                    "submitted",
                    error="",
                    note="human confirmed submission",
                )
            return self._transition(
                item.job_id,
                "prepared",
                note="human reported no submission",
            )

        if hold_seconds > 0:
            return self._transition(
                item.job_id,
                "prepared",
                note=(
                    f"browser held open for {hold_seconds}s; "
                    "submission not inferred"
                ),
            )
        return item

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _first(self, status: str) -> QueueItem | None:
        return next((item for item in self._items if item.status == status), None)

    def _find(self, job_id: int) -> QueueItem:
        for item in self._items:
            if item.job_id == job_id:
                return item
        raise LookupError(f"Queue item for job {job_id} not found")

    def _transition(
        self,
        job_id: int,
        status: str,
        *,
        resume_path: Any = _UNSET,
        error: Any = _UNSET,
        note: Any = _UNSET,
    ) -> QueueItem:
        if status not in VALID_STATUSES:
            raise ValueError(f"Invalid queue status: {status!r}")
        item = self._find(job_id)
        item.status = status
        if resume_path is not _UNSET:
            item.resume_path = str(resume_path or "")
        if error is not _UNSET:
            item.error = str(error or "")
        if note is not _UNSET:
            item.note = str(note or "")
        item.updated_at = utcnow()
        self._save()
        return item

    def _load(self) -> None:
        if not self.path.exists():
            return
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            raw_items = payload.get("items", [])
        elif isinstance(payload, list):
            raw_items = payload
        else:
            raise ValueError(f"Invalid queue file: {self.path}")
        if not isinstance(raw_items, list):
            raise ValueError(f"Invalid queue file: {self.path}")
        self._items = [QueueItem.from_dict(item) for item in raw_items]

    def _save(self) -> None:
        """Atomically persist the queue with owner-only permissions."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = (
            json.dumps(
                {
                    "version": QUEUE_VERSION,
                    "items": [item.to_dict() for item in self._items],
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n"
        )
        fd, tmp_name = tempfile.mkstemp(
            prefix=".queue-", suffix=".tmp", dir=str(self.path.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.path)
            os.chmod(self.path, 0o600)
        finally:
            tmp_path.unlink(missing_ok=True)


def _review_mode_config(config: Config) -> Config:
    """Return a config copy that cannot auto-submit from the queue."""
    if not config.browser.allow_submit:
        return config
    return replace(config, browser=replace(config.browser, allow_submit=False))
