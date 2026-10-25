from __future__ import annotations

from pathlib import Path

import pytest

from jobforge import db
from jobforge.models import Application, Job, MatchResult, Preferences, Profile
from jobforge.quality import QualityCheck, QualityReport, evaluate, is_ready

DESCRIPTION = (
    "We are hiring a software engineer to build and operate our local-first "
    "job search tools. You will work with Python, SQLite, and a browser "
    "automation layer. Requirements include clear writing, careful testing, "
    "and respect for human review gates. This description is deliberately "
    "long enough to clear the thin-JD warning."
)


def _seed(
    tmp_path: Path,
    *,
    description: str = DESCRIPTION,
    score: float = 80.0,
    gate_survival: float = 1.0,
    min_match: int = 55,
    one_per_company: bool = True,
    company: str = "Acme",
):
    path = tmp_path / "test.sqlite3"
    db.init_db(path)
    conn = db.connect(path)
    profile = Profile(name="tester", first_name="Test", last_name="User")
    profile.id = db.save_profile(conn, profile)
    job = Job(
        source="lever",
        source_id=f"{company}-1",
        company=company,
        title="Software Engineer",
        location="Remote",
        description=description,
    )
    job.id = db.upsert_job(conn, job)
    db.set_match(
        conn,
        MatchResult(
            job_id=job.id,
            profile_id=profile.id,
            score=score,
            coverage=0.8,
            gate_survival=gate_survival,
        ),
    )
    prefs = Preferences(min_match=min_match, one_application_per_company=one_per_company)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")
    cover = tmp_path / "cover.txt"
    cover.write_text("A short, truthful cover letter.", encoding="utf-8")
    return conn, job, profile, prefs, resume, cover


def _create_application(
    conn,
    job: Job,
    profile: Profile,
    resume: Path,
    cover: Path,
    **overrides,
) -> Application:
    data = {
        "job_id": job.id,
        "profile_id": profile.id,
        "status": "prepared",
        "ats": "lever",
        "resume_path": str(resume),
        "cover_letter_path": str(cover),
        "answers": {"Why this role?": "Local-first tools fit my background."},
    }
    data.update(overrides)
    app_id = db.create_application(conn, Application(**data))
    app = db.get_application(conn, app_id)
    assert app is not None
    return app


def _by_name(report: QualityReport, name: str) -> QualityCheck:
    return next(check for check in report.checks if check.name == name)


def _second_same_company_job(conn, profile: Profile, company: str = "Acme") -> Job:
    job = Job(
        source="lever",
        source_id=f"{company}-2",
        company=company,
        title="Backend Engineer",
        location="Remote",
        description=DESCRIPTION,
    )
    job.id = db.upsert_job(conn, job)
    db.set_match(
        conn,
        MatchResult(
            job_id=job.id,
            profile_id=profile.id,
            score=80,
            coverage=0.8,
            gate_survival=1.0,
        ),
    )
    return job


def test_ready_application_scores_100_and_is_ready(tmp_path: Path):
    conn, job, profile, prefs, resume, cover = _seed(tmp_path)
    application = _create_application(conn, job, profile, resume, cover)

    report = evaluate(conn, job, profile, prefs, application)

    assert report.ready is True
    assert report.score == 100
    assert is_ready(report) is True
    assert all(check.passed for check in report.checks)


def test_missing_application_blocks_as_not_prepared(tmp_path: Path):
    conn, job, profile, prefs, _resume, _cover = _seed(tmp_path)

    report = evaluate(conn, job, profile, prefs)

    assert report.ready is False
    assert is_ready(report) is False
    check = _by_name(report, "application_prepared")
    assert check.passed is False
    assert check.severity == "blocker"
    assert check.detail == "not prepared"
    assert _by_name(report, "resume").severity == "blocker"
    assert _by_name(report, "resume").passed is False


def test_missing_resume_file_blocks(tmp_path: Path):
    conn, job, profile, prefs, resume, cover = _seed(tmp_path)
    application = _create_application(
        conn, job, profile, resume, cover, resume_path=str(tmp_path / "missing.pdf")
    )

    report = evaluate(conn, job, profile, prefs, application)

    assert report.ready is False
    check = _by_name(report, "resume")
    assert check.passed is False
    assert check.severity == "blocker"
    assert "missing.pdf" in check.detail


def test_below_match_threshold_blocks(tmp_path: Path):
    conn, job, profile, prefs, resume, cover = _seed(tmp_path, score=40.0, min_match=55)
    application = _create_application(conn, job, profile, resume, cover)

    report = evaluate(conn, job, profile, prefs, application)

    assert report.ready is False
    assert report.score == 75
    check = _by_name(report, "match_score")
    assert check.passed is False
    assert check.severity == "blocker"
    assert "below minimum 55" in check.detail
    assert _by_name(report, "match_gate").passed is True


def test_zero_gate_survival_blocks(tmp_path: Path):
    conn, job, profile, prefs, resume, cover = _seed(tmp_path, gate_survival=0.0)
    application = _create_application(conn, job, profile, resume, cover)

    report = evaluate(conn, job, profile, prefs, application)

    assert report.ready is False
    assert report.score == 75
    check = _by_name(report, "match_gate")
    assert check.passed is False
    assert check.severity == "blocker"


def test_duplicate_company_is_blocker_when_one_per_company(tmp_path: Path):
    conn, job, profile, prefs, resume, cover = _seed(tmp_path)
    _create_application(conn, job, profile, resume, cover)

    second = _second_same_company_job(conn, profile)
    second_app = _create_application(conn, second, profile, resume, cover)

    report = evaluate(conn, second, profile, prefs, second_app)

    assert report.ready is False
    assert report.score == 75
    check = _by_name(report, "duplicate_company")
    assert check.passed is False
    assert check.severity == "blocker"
    assert "1 other active application" in check.detail


def test_duplicate_company_is_warning_when_allowed(tmp_path: Path):
    conn, job, profile, prefs, resume, cover = _seed(tmp_path, one_per_company=False)
    _create_application(conn, job, profile, resume, cover)

    second = _second_same_company_job(conn, profile)
    second_app = _create_application(conn, second, profile, resume, cover)

    report = evaluate(conn, second, profile, prefs, second_app)

    assert report.ready is True
    assert report.score == 95
    check = _by_name(report, "duplicate_company")
    assert check.passed is False
    assert check.severity == "warning"


def test_submitted_status_blocks_reapply(tmp_path: Path):
    conn, job, profile, prefs, resume, cover = _seed(tmp_path)
    application = _create_application(conn, job, profile, resume, cover, status="submitted")

    report = evaluate(conn, job, profile, prefs, application)

    assert report.ready is False
    assert report.score == 75
    check = _by_name(report, "application_status")
    assert check.passed is False
    assert check.severity == "blocker"
    assert "already submitted" in check.detail


def test_missing_cover_letter_and_answers_are_warnings(tmp_path: Path):
    conn, job, profile, prefs, resume, _cover = _seed(tmp_path)
    application = _create_application(
        conn,
        job,
        profile,
        resume,
        _cover,
        cover_letter_path="",
        answers={},
    )

    report = evaluate(conn, job, profile, prefs, application)

    assert report.ready is True
    assert report.score == 90
    for name in ("cover_letter", "answers"):
        check = _by_name(report, name)
        assert check.passed is False
        assert check.severity == "warning"


def test_captcha_error_code_blocks(tmp_path: Path):
    conn, job, profile, prefs, resume, cover = _seed(tmp_path)
    application = _create_application(conn, job, profile, resume, cover, error_code="captcha")

    report = evaluate(conn, job, profile, prefs, application)

    assert report.ready is False
    assert report.score == 75
    check = _by_name(report, "captcha")
    assert check.passed is False
    assert check.severity == "blocker"


def test_score_arithmetic_counts_blockers_and_warnings(tmp_path: Path):
    conn, job, profile, prefs, resume, _cover = _seed(
        tmp_path, description="Short JD", score=40.0, min_match=55
    )
    application = _create_application(
        conn, job, profile, resume, _cover, cover_letter_path=""
    )

    report = evaluate(conn, job, profile, prefs, application)

    # 1 blocker (match score) and 2 warnings (thin JD, missing cover letter).
    assert report.score == 100 - 25 * 1 - 5 * 2 == 65
    assert report.ready is False
    assert _by_name(report, "job_description").severity == "warning"
    assert _by_name(report, "job_description").passed is False


@pytest.mark.parametrize("status", ["submitted", "interview", "offer"])
def test_reapply_blocked_statuses(tmp_path: Path, status: str):
    conn, job, profile, prefs, resume, cover = _seed(tmp_path)
    application = _create_application(conn, job, profile, resume, cover, status=status)

    report = evaluate(conn, job, profile, prefs, application)

    assert report.ready is False
    assert _by_name(report, "application_status").passed is False


def test_preflight_skips_prepared_only_checks(tmp_path: Path):
    conn, job, profile, prefs, _resume, _cover = _seed(tmp_path)

    report = evaluate(conn, job, profile, prefs, require_prepared=False)

    assert report.ready is True
    names = {check.name for check in report.checks}
    assert "application_prepared" not in names
    assert "resume" not in names
    assert "cover_letter" not in names
    assert "answers" not in names


def test_preflight_still_blocks_low_match(tmp_path: Path):
    conn, job, profile, prefs, _resume, _cover = _seed(tmp_path, score=10.0)

    report = evaluate(conn, job, profile, prefs, require_prepared=False)

    assert report.ready is False
    assert _by_name(report, "match_score").passed is False


def test_quality_blocked_lists_blockers(tmp_path: Path):
    from jobforge.quality import QualityBlocked

    conn, job, profile, prefs, _resume, _cover = _seed(tmp_path, score=10.0)
    report = evaluate(conn, job, profile, prefs, require_prepared=False)

    exc = QualityBlocked(report)

    assert exc.report is report
    assert {check.name for check in exc.blocking} == {"match_score"}
    assert "match_score" in str(exc)
