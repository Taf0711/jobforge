#!/usr/bin/env python3
"""Deterministic offline end-to-end smoke for JobForge.

Starts the real FastAPI app on a free localhost port with a temporary database,
exercises the main web routes, runs a real Typst tailoring pass, updates the
application tracker, and prints one JSON verdict.

No network access, live ATS, browser, or email account is required.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Any

# Running ``python scripts/e2e_smoke.py`` puts ``scripts/`` on sys.path, not the
# repository root. Import the package from the checkout that owns this file.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import pymupdf  # noqa: E402
import uvicorn  # noqa: E402

from jobforge import db  # noqa: E402
from jobforge.config import (  # noqa: E402
    BrowserConfig,
    Config,
    EmailConfig,
    LLMConfig,
    ServerConfig,
)
from jobforge.models import ApplicationStatus, Job, Profile  # noqa: E402
from jobforge.web.app import create_app  # noqa: E402

CANDIDATE_FIRST = "Jane"
CANDIDATE_LAST = "Doe"
CANDIDATE_NAME = f"{CANDIDATE_FIRST} {CANDIDATE_LAST}"
JOB_DESCRIPTION = (
    "We are hiring a backend engineer to build APIs.\n\n"
    "Requirements: Python, FastAPI, PostgreSQL\n"
    "Nice to have: Docker"
)
PAGES = (
    "/",
    "/jobs",
    "/queue",
    "/applications",
    "/answers",
    "/profiles",
    "/settings",
    "/inbox",
    "/runs",
    "/api/status",
    "/api/answers",
)
HTTP_TIMEOUT_S = 5.0


def _check(checks: list[dict[str, Any]], name: str, passed: bool, detail: str = "") -> bool:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})
    return bool(passed)


def _build_config(tmp: Path, resume_path: Path, port: int) -> Config:
    config = Config(
        data_dir=tmp / "data",
        output_dir=tmp / "output",
        db_path=tmp / "data" / "jobforge.sqlite3",
        profile_path=None,
        resume_path=resume_path,
        sources={},
        preferences={},
        llm=LLMConfig(enabled=False),
        email=EmailConfig(enabled=False),
        browser=BrowserConfig(headless=True, allow_submit=False),
        server=ServerConfig(host="127.0.0.1", port=port),
    )
    config.ensure_dirs()
    return config


def _write_base_resume(path: Path) -> None:
    """Create the base resume in-process so the smoke has no fixture files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        CANDIDATE_NAME,
        "jane.doe@example.com | +1 555 123 4567 | New York, NY",
        "",
        "SUMMARY",
        "Backend engineer focused on Python APIs and reliable data systems.",
        "",
        "EXPERIENCE",
        "Software Engineer - Acme (2024 - Present)",
        "- Built a Python API that cut latency 40%.",
        "- Shipped a FastAPI service backed by PostgreSQL.",
        "",
        "EDUCATION",
        "Example University - Bachelor of Science in Computer Science (2022 - 2026)",
        "",
        "SKILLS",
        "Python, FastAPI, PostgreSQL, Docker",
    ]
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = 72.0
    for line in lines:
        page.insert_text((72, y), line, fontsize=10)
        y += 16
    doc.save(path)
    doc.close()


def _seed(conn: sqlite3.Connection, resume_path: Path) -> tuple[int, int]:
    profile = Profile(
        name="e2e",
        first_name=CANDIDATE_FIRST,
        last_name=CANDIDATE_LAST,
        email="jane.doe@example.com",
        phone="+1 555 123 4567",
        location="New York, NY",
        headline="Backend Engineer",
        summary="Backend engineer focused on Python APIs and reliable data systems.",
        resume_path=str(resume_path),
        resume_text="",
        skills=["Python", "FastAPI", "PostgreSQL", "Docker"],
        experience=[
            {
                "title": "Software Engineer",
                "company": "Acme",
                "location": "New York, NY",
                "start": "2024",
                "end": "Present",
                "bullets": [
                    "Built a Python API that cut latency 40%.",
                    "Shipped a FastAPI service backed by PostgreSQL.",
                ],
            }
        ],
        education=[
            {
                "institution": "Example University",
                "degree": "Bachelor of Science",
                "major": "Computer Science",
                "start": "2022",
                "end": "2026",
            }
        ],
        facts=[
            {
                "id": "fact-001",
                "section": "experience",
                "title": "Software Engineer",
                "company": "Acme",
                "text": "Built a Python API that cut latency 40%.",
                "skills": ["Python", "FastAPI"],
            },
            {
                "id": "fact-002",
                "section": "experience",
                "title": "Software Engineer",
                "company": "Acme",
                "text": "Shipped a FastAPI service backed by PostgreSQL.",
                "skills": ["FastAPI", "PostgreSQL"],
            },
        ],
    )
    profile_id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile_id)

    job_id = db.upsert_job(
        conn,
        Job(
            source="e2e",
            source_id="e2e-1",
            company="Acme",
            title="Backend Engineer",
            location="New York, NY",
            remote=False,
            url="https://example.com/jobs/e2e-1",
            apply_url="https://example.com/jobs/e2e-1/apply",
            description=JOB_DESCRIPTION,
        ),
    )
    return profile_id, job_id


def _start_server(config: Config, port: int, deadline: float) -> tuple[uvicorn.Server, threading.Thread]:
    app = create_app(config)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            log_config=None,
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(target=server.run, name="jobforge-e2e-server", daemon=True)
    thread.start()
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("uvicorn server thread exited before startup")
        if time.monotonic() > deadline:
            raise TimeoutError("uvicorn server did not start before the timeout")
        time.sleep(0.02)
    return server, thread


def _bound_port(server: uvicorn.Server, requested: int) -> int:
    sockets = server.servers[0].sockets if server.servers else []
    if not sockets:
        if requested:
            return requested
        raise RuntimeError("uvicorn reported no listening sockets")
    return int(sockets[0].getsockname()[1])


def _wait_for_health(client: httpx.Client, base_url: str, deadline: float) -> httpx.Response:
    last_error = ""
    while time.monotonic() <= deadline:
        try:
            response = client.get(f"{base_url}/healthz")
            if response.status_code == 200:
                return response
            last_error = f"status={response.status_code}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.05)
    raise TimeoutError(f"/healthz did not become ready before the timeout ({last_error})")


def _pdf_checks(pdf_path: Path, checks: list[dict[str, Any]]) -> None:
    exists = pdf_path.exists()
    _check(checks, "tailor-pdf-exists", exists, str(pdf_path))
    if not exists:
        return

    doc = pymupdf.open(pdf_path)
    try:
        pages = len(doc)
        text = "\n".join(page.get_text("text") for page in doc)
    finally:
        doc.close()

    _check(checks, "tailor-pdf-one-page", pages == 1, f"pages={pages}")
    _check(checks, "tailor-pdf-text-layer", len(text.strip()) >= 200, f"chars={len(text.strip())}")
    _check(
        checks,
        "tailor-pdf-candidate-name",
        CANDIDATE_NAME in text,
        f"looking for {CANDIDATE_NAME!r}",
    )


def run_e2e(port: int = 0, timeout_s: float = 30) -> dict[str, Any]:
    """Run the offline smoke and return a typed verdict.

    ``port=0`` binds an ephemeral localhost port. The temporary database and
    output directory are removed before returning; ``pdf`` records the path that
    existed during the run.
    """
    checks: list[dict[str, Any]] = []
    pdf_result = ""
    actual_port = 0
    tmp = Path(tempfile.mkdtemp(prefix="jobforge-e2e-"))
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    deadline = time.monotonic() + timeout_s

    try:
        typst = shutil.which("typst")
        if not _check(checks, "typst-available", typst is not None, typst or "typst not found on PATH"):
            return {"ok": False, "checks": checks, "port": 0, "pdf": ""}

        resume_path = tmp / "data" / "base_resume.pdf"
        _write_base_resume(resume_path)
        config = _build_config(tmp, resume_path, port)

        db.init_db(config.db_path)
        conn = db.connect(config.db_path)
        try:
            profile_id, job_id = _seed(conn, resume_path)
        finally:
            conn.close()

        server, thread = _start_server(config, port, deadline)
        actual_port = _bound_port(server, port)
        base_url = f"http://127.0.0.1:{actual_port}"
        limits = httpx.Timeout(HTTP_TIMEOUT_S)
        with httpx.Client(timeout=limits, trust_env=False, follow_redirects=False) as client:
            health = _wait_for_health(client, base_url, deadline)
            _check(checks, "healthz", health.status_code == 200, f"status={health.status_code}")

            for path in PAGES:
                response = client.get(base_url + path)
                _check(checks, f"GET {path}", response.status_code == 200, f"status={response.status_code}")

            response = client.get(f"{base_url}/jobs/{job_id}")
            _check(checks, f"GET /jobs/{{id}}", response.status_code == 200, f"status={response.status_code}")
            _check(checks, "job-detail-title", "Backend Engineer" in response.text, f"job_id={job_id}")

            response = client.post(f"{base_url}/jobs/{job_id}/tailor")
            location = response.headers.get("location", "")
            _check(
                checks,
                "POST /jobs/{id}/tailor",
                response.status_code in (200, 303) and "Tailor+failed" not in location,
                f"status={response.status_code} location={location}",
            )

            conn = db.connect(config.db_path)
            try:
                row = conn.execute(
                    "SELECT * FROM applications WHERE job_id=? AND profile_id=? ORDER BY id DESC LIMIT 1",
                    (job_id, profile_id),
                ).fetchone()
            finally:
                conn.close()
            if row is None:
                raise RuntimeError("tailor route returned but no application row was created")
            app_id = int(row["id"])
            _check(checks, "tailor-application", True, f"application_id={app_id} status={row['status']}")

            pdf_path = Path(str(row["resume_path"]))
            pdf_result = str(pdf_path)
            _check(
                checks,
                "tailor-pdf-under-output",
                pdf_path.resolve().is_relative_to(config.output_dir.resolve()),
                f"output_dir={config.output_dir} pdf={pdf_path}",
            )
            _pdf_checks(pdf_path, checks)

            new_status = ApplicationStatus.INTERVIEW.value
            response = client.post(
                f"{base_url}/applications/{app_id}/status",
                data={"status": new_status},
            )
            _check(
                checks,
                "POST /applications/{id}/status",
                response.status_code in (200, 303),
                f"status={response.status_code}",
            )

            conn = db.connect(config.db_path)
            try:
                stored = db.get_application(conn, app_id)
            finally:
                conn.close()
            _check(
                checks,
                "application-status-persisted",
                stored is not None and stored.status == new_status,
                f"stored={stored.status if stored else None} expected={new_status}",
            )

            tracker = client.get(f"{base_url}/applications")
            _check(
                checks,
                "tracker-reflects-application",
                tracker.status_code == 200 and "Acme" in tracker.text,
                f"status={tracker.status_code}",
            )
    except Exception:
        _check(checks, "exception", False, traceback.format_exc())
    finally:
        if server is not None:
            server.should_exit = True
            if thread is not None:
                thread.join(timeout=5)
                if thread.is_alive():
                    server.force_exit = True
                    thread.join(timeout=2)
        shutil.rmtree(tmp, ignore_errors=True)

    return {
        "ok": all(item["passed"] for item in checks),
        "checks": checks,
        "port": actual_port,
        "pdf": pdf_result,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic offline JobForge smoke.")
    parser.add_argument("--json", action="store_true", help="print one compact JSON document")
    parser.add_argument("--port", type=int, default=0, help="localhost port (0 picks a free port)")
    parser.add_argument("--timeout", type=float, default=30, help="startup/health timeout in seconds")
    args = parser.parse_args(argv)

    verdict = run_e2e(port=args.port, timeout_s=args.timeout)
    if args.json:
        print(json.dumps(verdict, separators=(",", ":")))
    else:
        for item in verdict["checks"]:
            mark = "PASS" if item["passed"] else "FAIL"
            print(f"[{mark}] {item['name']}: {item['detail']}")
        print(f"ok={verdict['ok']} port={verdict['port']} pdf={verdict['pdf']}")
    return 0 if verdict["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
