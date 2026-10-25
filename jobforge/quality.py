"""Pre-submit quality gate.

The gate is deterministic and local: it reads the stored match, the prepared
application, and the duplicate-application history. It never calls an LLM and
never opens a browser.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import db
from .models import Application, ApplicationStatus, Job, Preferences, Profile

BLOCKER = "blocker"
WARNING = "warning"

REAPPLY_STATUSES = {
    ApplicationStatus.SUBMITTED.value,
    ApplicationStatus.INTERVIEW.value,
    ApplicationStatus.OFFER.value,
}

THIN_DESCRIPTION_CHARS = 200


@dataclass
class QualityCheck:
    name: str
    passed: bool
    severity: str
    detail: str


@dataclass
class QualityReport:
    score: int
    ready: bool
    checks: list[QualityCheck]


def blocking_checks(report: QualityReport) -> list[QualityCheck]:
    """Return only failed blocker checks from a report."""
    return [check for check in report.checks if not check.passed and check.severity == BLOCKER]


class QualityBlocked(RuntimeError):
    """Raised when a browser-opening action fails the quality gate."""

    def __init__(self, report: QualityReport):
        self.report = report
        self.blocking = blocking_checks(report)
        detail = "; ".join(f"{check.name}: {check.detail}" for check in self.blocking)
        super().__init__(f"Quality gate blocked: {detail}" if detail else "Quality gate blocked")


def is_ready(report: QualityReport) -> bool:
    """Return whether the report has no failed blocker checks."""
    return report.ready


def _file_exists(path: str) -> bool:
    if not path:
        return False
    try:
        return Path(path).is_file()
    except (OSError, ValueError):
        return False


def _load_application(
    conn: sqlite3.Connection,
    job: Job,
    profile: Profile,
    application: Application | None,
) -> Application | None:
    if application is not None or job.id is None or profile.id is None:
        return application
    row = conn.execute(
        "SELECT id FROM applications WHERE job_id=? AND profile_id=?",
        (job.id, profile.id),
    ).fetchone()
    if row is None:
        return None
    return db.get_application(conn, int(row["id"]))


def _check_match_score(
    match, prefs: Preferences
) -> QualityCheck:
    if match is None:
        return QualityCheck(
            name="match_score",
            passed=False,
            severity=BLOCKER,
            detail="no match score; run score first",
        )
    if match.score < prefs.min_match:
        return QualityCheck(
            name="match_score",
            passed=False,
            severity=BLOCKER,
            detail=f"match {match.score:g} is below minimum {prefs.min_match}",
        )
    return QualityCheck(
        name="match_score",
        passed=True,
        severity=BLOCKER,
        detail=f"match {match.score:g} meets minimum {prefs.min_match}",
    )


def _check_match_gates(match) -> QualityCheck:
    if match is None:
        return QualityCheck(
            name="match_gate",
            passed=False,
            severity=BLOCKER,
            detail="no match score; gate survival unknown",
        )
    if match.gate_survival <= 0:
        return QualityCheck(
            name="match_gate",
            passed=False,
            severity=BLOCKER,
            detail=f"gate survival {match.gate_survival:g}",
        )
    return QualityCheck(
        name="match_gate",
        passed=True,
        severity=BLOCKER,
        detail=f"gate survival {match.gate_survival:g}",
    )


def _check_description(job: Job) -> QualityCheck:
    length = len((job.description or "").strip())
    if length < THIN_DESCRIPTION_CHARS:
        return QualityCheck(
            name="job_description",
            passed=False,
            severity=WARNING,
            detail=f"description is {length} characters; thin JDs are low-confidence",
        )
    return QualityCheck(
        name="job_description",
        passed=True,
        severity=WARNING,
        detail=f"description is {length} characters",
    )


def _check_application(application: Application | None) -> QualityCheck:
    if application is None:
        return QualityCheck(
            name="application_prepared",
            passed=False,
            severity=BLOCKER,
            detail="not prepared",
        )
    return QualityCheck(
        name="application_prepared",
        passed=True,
        severity=BLOCKER,
        detail=f"application {application.id if application.id is not None else ''}".strip(),
    )


def _check_resume(application: Application | None) -> QualityCheck:
    path = application.resume_path if application else ""
    if not path:
        return QualityCheck(
            name="resume",
            passed=False,
            severity=BLOCKER,
            detail="resume_path is empty",
        )
    if not _file_exists(path):
        return QualityCheck(
            name="resume",
            passed=False,
            severity=BLOCKER,
            detail=f"resume not found: {path}",
        )
    return QualityCheck(
        name="resume",
        passed=True,
        severity=BLOCKER,
        detail=path,
    )


def _check_duplicate_company(
    conn: sqlite3.Connection,
    job: Job,
    profile: Profile,
    prefs: Preferences,
    application: Application | None,
) -> QualityCheck:
    severity = BLOCKER if prefs.one_application_per_company else WARNING
    if profile.id is None or not (job.company or "").strip():
        return QualityCheck(
            name="duplicate_company",
            passed=True,
            severity=severity,
            detail="no company to compare",
        )
    app_id = application.id if application is not None else None
    rows = conn.execute(
        """
        SELECT a.id, a.status, j.title
        FROM applications a JOIN jobs j ON j.id = a.job_id
        WHERE a.profile_id=?
          AND lower(trim(j.company)) = lower(trim(?))
          AND a.status NOT IN ('withdrawn')
          AND (? IS NULL OR a.id != ?)
        ORDER BY a.id
        """,
        (profile.id, job.company, app_id, app_id),
    ).fetchall()
    if not rows:
        return QualityCheck(
            name="duplicate_company",
            passed=True,
            severity=severity,
            detail=f"no other active applications at {job.company}",
        )
    labels = ", ".join(f"{row['title']} ({row['status']})" for row in rows)
    return QualityCheck(
        name="duplicate_company",
        passed=False,
        severity=severity,
        detail=f"{len(rows)} other active application(s) at {job.company}: {labels}",
    )


def _check_status(application: Application | None) -> QualityCheck:
    if application is None:
        return QualityCheck(
            name="application_status",
            passed=True,
            severity=BLOCKER,
            detail="no application status yet",
        )
    if application.status in REAPPLY_STATUSES:
        return QualityCheck(
            name="application_status",
            passed=False,
            severity=BLOCKER,
            detail=f"already {application.status}; re-apply is blocked",
        )
    return QualityCheck(
        name="application_status",
        passed=True,
        severity=BLOCKER,
        detail=f"status {application.status}",
    )


def _check_cover_letter(application: Application | None) -> QualityCheck:
    path = application.cover_letter_path if application else ""
    if not path:
        return QualityCheck(
            name="cover_letter",
            passed=False,
            severity=WARNING,
            detail="cover letter path is empty",
        )
    if not _file_exists(path):
        return QualityCheck(
            name="cover_letter",
            passed=False,
            severity=WARNING,
            detail=f"cover letter not found: {path}",
        )
    return QualityCheck(
        name="cover_letter",
        passed=True,
        severity=WARNING,
        detail=path,
    )


def _check_answers(application: Application | None) -> QualityCheck:
    answers = application.answers if application else {}
    if not answers:
        return QualityCheck(
            name="answers",
            passed=False,
            severity=WARNING,
            detail="answers are empty",
        )
    return QualityCheck(
        name="answers",
        passed=True,
        severity=WARNING,
        detail=f"{len(answers)} answer(s)",
    )


def _check_captcha(application: Application | None) -> QualityCheck:
    code = (application.error_code if application else "").strip().lower()
    if code == "captcha":
        return QualityCheck(
            name="captcha",
            passed=False,
            severity=BLOCKER,
            detail="captcha handoff recorded; human must clear it",
        )
    return QualityCheck(
        name="captcha",
        passed=True,
        severity=BLOCKER,
        detail="no captcha handoff",
    )


def evaluate(
    conn: sqlite3.Connection,
    job: Job,
    profile: Profile,
    prefs: Preferences,
    application: Application | None = None,
    *,
    require_prepared: bool = True,
) -> QualityReport:
    """Evaluate the pre-submit quality gate for one job and profile.

    ``require_prepared=False`` runs the preflight subset used before a direct
    apply: it keeps the hard pre-browser blockers (match score, gate survival,
    duplicate company, already-submitted status, captcha) but does not require
    that tailoring already produced an application row. The queue uses the
    default full report after ``prepare_next``.
    """
    application = _load_application(conn, job, profile, application)
    match = None
    if job.id is not None and profile.id is not None:
        match = db.get_match(conn, job.id, profile.id)

    checks = [
        _check_match_score(match, prefs),
        _check_match_gates(match),
        _check_description(job),
    ]
    if require_prepared:
        checks.extend([_check_application(application), _check_resume(application)])
    checks.extend(
        [
            _check_duplicate_company(conn, job, profile, prefs, application),
            _check_status(application),
        ]
    )
    if require_prepared:
        checks.extend([_check_cover_letter(application), _check_answers(application)])
    checks.append(_check_captcha(application))

    failed = [check for check in checks if not check.passed]
    blockers = sum(1 for check in failed if check.severity == BLOCKER)
    warnings = sum(1 for check in failed if check.severity == WARNING)
    score = max(0, 100 - 25 * blockers - 5 * warnings)
    return QualityReport(score=score, ready=blockers == 0, checks=checks)
