"""Feature-parity audit against the decomposition matrix."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from ..config import Config
from ..sources.registry import SOURCES

ROOT = Path(__file__).resolve().parent.parent.parent
MATRIX = ROOT / "docs" / "02-feature-matrix.md"

CHECKS: list[tuple[str, str, str]] = [
    ("Resume upload/parse", "jobforge/matching/resume.py", "profile_from_resume"),
    ("Profile import", "jobforge/web/app.py", "/profiles/import"),
    ("Multiple profiles", "jobforge/db.py", "CREATE TABLE IF NOT EXISTS profiles"),
    ("Preferences", "jobforge/models.py", "class Preferences"),
    ("Job feed", "jobforge/sources/registry.py", "fetch_configured"),
    ("Feed filters", "jobforge/db.py", "def list_jobs"),
    ("Match score", "jobforge/matching/score.py", "def score_job"),
    ("Swipe/apply UI", "jobforge/web/templates/job_detail.html", "Fill application"),
    ("Saved jobs", "jobforge/db.py", "CREATE TABLE IF NOT EXISTS saved_jobs"),
    ("Job detail", "jobforge/web/templates/job_detail.html", "Job description"),
    ("Paste a URL", "jobforge/sources/direct.py", "def fetch_job_url"),
    ("One app per company", "jobforge/db.py", "one_per_company"),
    ("Applied suppression", "jobforge/db.py", "CREATE TABLE IF NOT EXISTS applications"),
    ("Review before submit", "jobforge/automation/base.py", "hold_seconds"),
    ("Run log", "jobforge/db.py", "CREATE TABLE IF NOT EXISTS runs"),
    ("Tailor per job", "jobforge/tailoring/tailor.py", "def tailor_resume"),
    ("Greenhouse adapter", "jobforge/automation/greenhouse.py", "class GreenhouseAdapter"),
    ("Lever adapter", "jobforge/automation/lever.py", "class LeverAdapter"),
    ("Ashby adapter", "jobforge/automation/ashby.py", "class AshbyAdapter"),
    ("Workable adapter", "jobforge/automation/workable.py", "class WorkableAdapter"),
    ("Answer logging", "jobforge/automation/base.py", "class FillReport"),
    ("Failure taxonomy", "jobforge/automation/detect.py", "captcha_present"),
    ("Screenshot", "jobforge/automation/base.py", "page.screenshot"),
    ("Retry/backoff", "jobforge/sources/base.py", "for attempt in range(retries)"),
    ("Private email / IMAP", "jobforge/email/imap.py", "def poll"),
    ("Verification codes", "jobforge/email/imap.py", "def extract_code"),
    ("Reply classification", "jobforge/email/imap.py", "def classify"),
    ("Tracker", "jobforge/web/templates/applications.html", "Applications"),
    ("Status history", "jobforge/db.py", "CREATE TABLE IF NOT EXISTS events"),
    ("Inbox UI", "jobforge/web/templates/inbox.html", "Inbox"),
    ("Resume Tailor", "jobforge/tailoring/tailor.py", "select_facts"),
    ("Cover letter", "jobforge/tailoring/tailor.py", "def cover_letter"),
    ("Fact audit", "jobforge/tailoring/tailor.py", "def _supported"),
    ("ATS PDF check", "jobforge/tailoring/ats_check.py", "def ats_check"),
    ("Prompt-injection guard", "jobforge/tailoring/llm.py", "GROUNDING_SYSTEM"),
    ("SSRF guard", "jobforge/sources/base.py", "def assert_public_url"),
    ("Dedup/fingerprint", "jobforge/models.py", "fingerprint"),
    ("Export", "jobforge/services.py", "def export_data"),
    ("Audit log", "jobforge/db.py", "CREATE TABLE IF NOT EXISTS events"),
    ("Rate limiting", "jobforge/sources/base.py", "class RateLimiter"),
    ("Offline mode", "jobforge/db.py", "sqlite3"),
    ("GitHub repository context", "jobforge/integrations/github.py", "def connect_github"),
    ("Agent runtime", "jobforge/agent/runtime.py", "class AgentRuntime"),
    ("Model providers", "jobforge/agent/providers.py", "PROVIDERS"),
    ("Candidate context", "jobforge/agent/context.py", "build_candidate_context"),
    ("Agent thread persistence", "jobforge/db.py", "CREATE TABLE IF NOT EXISTS agent_threads"),
    ("Agent chat UI", "jobforge/web/templates/agent.html", "agent-form"),
]


def _matrix_counts() -> dict[str, int]:
    if not MATRIX.exists():
        return {}
    text = MATRIX.read_text(encoding="utf-8")
    counts = {"v1": 0, "v2": 0, "never": 0}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        scope = cells[4].lower()
        for key in counts:
            if scope.startswith(key):
                counts[key] += 1
    return counts


def run_parity(config: Config, conn: sqlite3.Connection) -> dict[str, Any]:
    results = []
    missing = []
    for feature, rel_path, needle in CHECKS:
        path = ROOT / rel_path
        found = path.exists() and needle in path.read_text(encoding="utf-8", errors="replace")
        results.append({"feature": feature, "file": rel_path, "found": found})
        if not found:
            missing.append(feature)

    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    adapters = [name for name in SOURCES]
    counts = _matrix_counts()
    implemented = sum(1 for r in results if r["found"])
    return {
        "matrix": counts,
        "checks": results,
        "implemented": implemented,
        "total_checks": len(results),
        "coverage_pct": round(100 * implemented / len(results), 1) if results else 0,
        "missing": missing,
        "sources": adapters,
        "tables": sorted(tables),
        "note": "Heuristic presence check, not a behavioral test. Pair with pytest and a manual smoke run.",
    }
