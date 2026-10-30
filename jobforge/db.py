"""SQLite persistence layer. Stdlib only, parameterized queries everywhere."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import (
    Application,
    ApplicationStatus,
    EmailMessage,
    Job,
    MatchResult,
    Preferences,
    Profile,
    RunStatus,
    utcnow,
)

SCHEMA_VERSION = 6

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    remote INTEGER NOT NULL DEFAULT 0,
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency TEXT NOT NULL DEFAULT 'USD',
    url TEXT NOT NULL DEFAULT '',
    apply_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    posted_at TEXT,
    discovered_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    raw_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted_at);

CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    resume_path TEXT NOT NULL DEFAULT '',
    resume_text TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS preferences (
    profile_id INTEGER PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
    data_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS matches (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    score REAL NOT NULL,
    coverage REAL NOT NULL,
    gate_survival REAL NOT NULL,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    gaps_json TEXT NOT NULL DEFAULT '[]',
    requirements_json TEXT NOT NULL DEFAULT '[]',
    meta_json TEXT NOT NULL DEFAULT '{}',
    model_version TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    PRIMARY KEY (job_id, profile_id)
);
CREATE INDEX IF NOT EXISTS idx_matches_score ON matches(profile_id, score);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'draft',
    ats TEXT NOT NULL DEFAULT '',
    resume_path TEXT NOT NULL DEFAULT '',
    cover_letter_path TEXT NOT NULL DEFAULT '',
    answers_json TEXT NOT NULL DEFAULT '{}',
    screenshot_path TEXT NOT NULL DEFAULT '',
    error_code TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    submitted_at TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE(job_id, profile_id)
);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(profile_id, status);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER REFERENCES applications(id) ON DELETE CASCADE,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_app ON events(application_id, at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
    message_id TEXT NOT NULL DEFAULT '' UNIQUE,
    sender TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'other',
    verification_code TEXT NOT NULL DEFAULT '',
    forwarded INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS saved_jobs (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    saved_at TEXT NOT NULL,
    PRIMARY KEY (job_id, profile_id)
);

CREATE TABLE IF NOT EXISTS hidden_jobs (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    hidden_at TEXT NOT NULL,
    PRIMARY KEY (job_id, profile_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS agent_threads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    title TEXT NOT NULL DEFAULT 'New conversation',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_threads_profile ON agent_threads(profile_id, updated_at);

CREATE TABLE IF NOT EXISTS agent_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL DEFAULT '',
    tool_call_id TEXT NOT NULL DEFAULT '',
    tool_calls_json TEXT NOT NULL DEFAULT '[]',
    usage_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_messages_thread ON agent_messages(thread_id, id);

CREATE TABLE IF NOT EXISTS connections (
    provider TEXT PRIMARY KEY,
    account_id TEXT NOT NULL DEFAULT '',
    login TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    scopes TEXT NOT NULL DEFAULT '',
    token_backend TEXT NOT NULL DEFAULT '',
    connected_at TEXT NOT NULL,
    last_sync_at TEXT,
    meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS agent_instances (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL DEFAULT 'local',
    created_at TEXT NOT NULL,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_connections (
    id TEXT PRIMARY KEY,
    instance_id TEXT NOT NULL REFERENCES agent_instances(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    client TEXT NOT NULL DEFAULT 'generic',
    label TEXT NOT NULL DEFAULT '',
    scopes_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 0,
    generation INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_connections_profile ON agent_connections(profile_id);
CREATE INDEX IF NOT EXISTS idx_agent_connections_instance ON agent_connections(instance_id);

CREATE TABLE IF NOT EXISTS agent_memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    connection_id TEXT NOT NULL REFERENCES agent_connections(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(profile_id, key)
);
CREATE INDEX IF NOT EXISTS idx_agent_memories_profile ON agent_memories(profile_id);

CREATE TABLE IF NOT EXISTS agent_operations (
    id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL REFERENCES agent_connections(id) ON DELETE CASCADE,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    approved_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    owner_nonce TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    UNIQUE(connection_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_agent_operations_state ON agent_operations(state, created_at);
CREATE INDEX IF NOT EXISTS idx_agent_operations_profile ON agent_operations(profile_id);

CREATE TABLE IF NOT EXISTS agent_operation_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES agent_operations(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(operation_id, seq)
);

CREATE TABLE IF NOT EXISTS github_repos (
    repo_id INTEGER PRIMARY KEY,
    owner TEXT NOT NULL DEFAULT '',
    full_name TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    html_url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    homepage TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '',
    languages_json TEXT NOT NULL DEFAULT '{}',
    topics_json TEXT NOT NULL DEFAULT '[]',
    stars INTEGER NOT NULL DEFAULT 0,
    forks INTEGER NOT NULL DEFAULT 0,
    private INTEGER NOT NULL DEFAULT 0,
    fork INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    pushed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    readme TEXT NOT NULL DEFAULT '',
    tech_line TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    suggested_bullets_json TEXT NOT NULL DEFAULT '[]',
    ignored INTEGER NOT NULL DEFAULT 0,
    synced_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_github_repos_pushed ON github_repos(pushed_at);
CREATE INDEX IF NOT EXISTS idx_github_repos_name ON github_repos(full_name);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def connect_readonly(path: str | Path) -> sqlite3.Connection:
    """Open an existing database without creating schema or changing journal mode."""
    db_path = Path(path)
    if not db_path.exists():
        raise FileNotFoundError(str(db_path))
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as conn:
        conn.executescript(SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(matches)").fetchall()}
        if "meta_json" not in columns:
            conn.execute("ALTER TABLE matches ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}'")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        conn.commit()


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _job_from_row(row: sqlite3.Row) -> Job:
    return Job(
        id=row["id"],
        source=row["source"],
        source_id=row["source_id"],
        company=row["company"],
        title=row["title"],
        location=row["location"],
        remote=bool(row["remote"]),
        salary_min=row["salary_min"],
        salary_max=row["salary_max"],
        salary_currency=row["salary_currency"],
        url=row["url"],
        apply_url=row["apply_url"],
        description=row["description"],
        posted_at=row["posted_at"],
        discovered_at=row["discovered_at"],
        fingerprint=row["fingerprint"],
        raw=json.loads(row["raw_json"] or "{}"),
    )


def upsert_job(conn: sqlite3.Connection, job: Job) -> int:
    conn.execute(
        """
        INSERT INTO jobs (source, source_id, company, title, location, remote,
                          salary_min, salary_max, salary_currency, url, apply_url,
                          description, posted_at, discovered_at, fingerprint, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(fingerprint) DO UPDATE SET
            source=excluded.source,
            source_id=excluded.source_id,
            company=excluded.company,
            title=excluded.title,
            location=excluded.location,
            remote=excluded.remote,
            salary_min=COALESCE(excluded.salary_min, jobs.salary_min),
            salary_max=COALESCE(excluded.salary_max, jobs.salary_max),
            salary_currency=excluded.salary_currency,
            url=excluded.url,
            apply_url=excluded.apply_url,
            description=excluded.description,
            posted_at=COALESCE(excluded.posted_at, jobs.posted_at),
            raw_json=excluded.raw_json
        """,
        (
            job.source,
            job.source_id,
            job.company,
            job.title,
            job.location,
            int(job.remote),
            job.salary_min,
            job.salary_max,
            job.salary_currency,
            job.url,
            job.apply_url,
            job.description,
            job.posted_at,
            job.discovered_at,
            job.fingerprint,
            json.dumps(job.raw, ensure_ascii=False),
        ),
    )
    row = conn.execute("SELECT id FROM jobs WHERE fingerprint=?", (job.fingerprint,)).fetchone()
    conn.commit()
    return int(row["id"])


def get_job(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _job_from_row(row) if row else None


def list_jobs(
    conn: sqlite3.Connection,
    profile_id: int | None = None,
    q: str = "",
    min_score: float | None = None,
    source: str = "",
    status: str = "",
    include_hidden: bool = False,
    one_per_company: bool = False,
    limit: int = 100,
    offset: int = 0,
    exclude_sources: list[str] | None = None,
) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: list[Any] = []
    if q:
        where.append("(j.title LIKE ? OR j.company LIKE ? OR j.description LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if source:
        where.append("j.source=?")
        params.append(source)
    else:
        # Imported resume-forge tracker rows are historical applications, not
        # live opportunities. They stay addressable by id and by an explicit
        # source filter, but they do not enter the feed or queue by default.
        where.append("j.source <> 'resume-forge'")
    if exclude_sources:
        placeholders = ",".join("?" for _ in exclude_sources)
        where.append(f"j.source NOT IN ({placeholders})")
        params.extend(exclude_sources)
    if min_score is not None:
        where.append("COALESCE(m.score,0) >= ?")
        params.append(min_score)
    if status:
        where.append("a.status=?")
        params.append(status)
    if one_per_company and profile_id is not None:
        where.append(
            "NOT EXISTS (SELECT 1 FROM applications a2 JOIN jobs j2 ON j2.id=a2.job_id "
            "WHERE a2.profile_id=? AND lower(j2.company)=lower(j.company) "
            "AND a2.status NOT IN ('withdrawn'))"
        )
        params.append(profile_id)
    if not include_hidden and profile_id is not None:
        where.append("h.job_id IS NULL")
    if profile_id is None:
        profile_id = -1

    sql = f"""
        SELECT j.*, m.score, m.coverage, m.gate_survival, m.reasons_json, m.gaps_json,
               m.requirements_json, m.meta_json, m.scored_at,
               a.id AS application_id, a.status AS application_status,
               CASE WHEN s.job_id IS NULL THEN 0 ELSE 1 END AS saved
        FROM jobs j
        LEFT JOIN matches m ON m.job_id=j.id AND m.profile_id=?
        LEFT JOIN applications a ON a.job_id=j.id AND a.profile_id=?
        LEFT JOIN saved_jobs s ON s.job_id=j.id AND s.profile_id=?
        LEFT JOIN hidden_jobs h ON h.job_id=j.id AND h.profile_id=?
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(m.score, 0) DESC, COALESCE(j.posted_at, j.discovered_at) DESC
        LIMIT ? OFFSET ?
    """
    rows = conn.execute(sql, [profile_id, profile_id, profile_id, profile_id, *params, limit, offset]).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        d["remote"] = bool(d["remote"])
        d["reasons"] = json.loads(d.pop("reasons_json") or "[]")
        d["gaps"] = json.loads(d.pop("gaps_json") or "[]")
        d["requirements"] = json.loads(d.pop("requirements_json") or "[]")
        d["meta"] = json.loads(d.pop("meta_json") or "{}")
        d["saved"] = bool(d["saved"])
        out.append(d)
    return out


def count_jobs(conn: sqlite3.Connection, profile_id: int | None = None) -> int:
    if profile_id is None:
        row = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE source <> 'resume-forge'").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs j LEFT JOIN hidden_jobs h "
            "ON h.job_id=j.id AND h.profile_id=? "
            "WHERE h.job_id IS NULL AND j.source <> 'resume-forge'",
            (profile_id,),
        ).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# Matches
# ---------------------------------------------------------------------------


def set_match(conn: sqlite3.Connection, m: MatchResult) -> None:
    conn.execute(
        """
        INSERT INTO matches (job_id, profile_id, score, coverage, gate_survival,
                             reasons_json, gaps_json, requirements_json, meta_json,
                             model_version, scored_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(job_id, profile_id) DO UPDATE SET
            score=excluded.score, coverage=excluded.coverage,
            gate_survival=excluded.gate_survival, reasons_json=excluded.reasons_json,
            gaps_json=excluded.gaps_json, requirements_json=excluded.requirements_json,
            meta_json=excluded.meta_json,
            model_version=excluded.model_version, scored_at=excluded.scored_at
        """,
        (
            m.job_id,
            m.profile_id,
            m.score,
            m.coverage,
            m.gate_survival,
            json.dumps(m.reasons, ensure_ascii=False),
            json.dumps(m.gaps, ensure_ascii=False),
            json.dumps(m.requirements, ensure_ascii=False),
            json.dumps(m.meta, ensure_ascii=False),
            m.model_version,
            utcnow(),
        ),
    )
    conn.commit()


def get_match(conn: sqlite3.Connection, job_id: int, profile_id: int) -> MatchResult | None:
    row = conn.execute("SELECT * FROM matches WHERE job_id=? AND profile_id=?", (job_id, profile_id)).fetchone()
    if not row:
        return None
    return MatchResult(
        job_id=row["job_id"],
        profile_id=row["profile_id"],
        score=row["score"],
        coverage=row["coverage"],
        gate_survival=row["gate_survival"],
        reasons=json.loads(row["reasons_json"] or "[]"),
        gaps=json.loads(row["gaps_json"] or "[]"),
        requirements=json.loads(row["requirements_json"] or "[]"),
        meta=json.loads(row["meta_json"] or "{}") if "meta_json" in row.keys() else {},
        model_version=row["model_version"],
    )


# ---------------------------------------------------------------------------
# Profiles and preferences
# ---------------------------------------------------------------------------


def save_profile(conn: sqlite3.Connection, profile: Profile) -> int:
    now = utcnow()
    data = profile.to_dict()
    data.pop("id", None)
    data.pop("is_active", None)
    payload = json.dumps(data, ensure_ascii=False)
    if profile.id:
        conn.execute(
            "UPDATE profiles SET name=?, resume_path=?, resume_text=?, data_json=?, updated_at=? WHERE id=?",
            (profile.name, profile.resume_path, profile.resume_text, payload, now, profile.id),
        )
        pid = profile.id
    else:
        cur = conn.execute(
            "INSERT INTO profiles (name, is_active, resume_path, resume_text, data_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (profile.name, 0, profile.resume_path, profile.resume_text, payload, now, now),
        )
        pid = int(cur.lastrowid)
    conn.commit()
    return pid


def _profile_from_row(row: sqlite3.Row) -> Profile:
    data = json.loads(row["data_json"] or "{}")
    data.update(
        {
            "id": row["id"],
            "name": row["name"],
            "is_active": bool(row["is_active"]),
            "resume_path": row["resume_path"],
            "resume_text": row["resume_text"],
        }
    )
    return Profile.from_dict(data)


def get_profile(conn: sqlite3.Connection, profile_id: int) -> Profile | None:
    row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    return _profile_from_row(row) if row else None


def get_active_profile(conn: sqlite3.Connection) -> Profile | None:
    row = conn.execute("SELECT * FROM profiles WHERE is_active=1 ORDER BY id LIMIT 1").fetchone()
    if row:
        return _profile_from_row(row)
    row = conn.execute("SELECT * FROM profiles ORDER BY id LIMIT 1").fetchone()
    return _profile_from_row(row) if row else None


def list_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, name, is_active, resume_path, updated_at FROM profiles ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def set_active_profile(conn: sqlite3.Connection, profile_id: int) -> None:
    conn.execute("UPDATE profiles SET is_active=0")
    conn.execute("UPDATE profiles SET is_active=1 WHERE id=?", (profile_id,))
    conn.commit()


def save_preferences(conn: sqlite3.Connection, profile_id: int, prefs: Preferences) -> None:
    conn.execute(
        "INSERT INTO preferences (profile_id, data_json) VALUES (?,?) "
        "ON CONFLICT(profile_id) DO UPDATE SET data_json=excluded.data_json",
        (profile_id, json.dumps(prefs.to_dict(), ensure_ascii=False)),
    )
    conn.commit()


def get_preferences(conn: sqlite3.Connection, profile_id: int) -> Preferences:
    row = conn.execute("SELECT data_json FROM preferences WHERE profile_id=?", (profile_id,)).fetchone()
    if not row:
        return Preferences()
    return Preferences.from_dict(json.loads(row["data_json"] or "{}"))


# ---------------------------------------------------------------------------
# Applications and events
# ---------------------------------------------------------------------------


def create_application(conn: sqlite3.Connection, app: Application) -> int:
    now = utcnow()
    cur = conn.execute(
        """
        INSERT INTO applications (job_id, profile_id, status, ats, resume_path, cover_letter_path,
                                  answers_json, screenshot_path, error_code, notes,
                                  created_at, submitted_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(job_id, profile_id) DO UPDATE SET
            status=excluded.status, ats=excluded.ats, resume_path=excluded.resume_path,
            cover_letter_path=excluded.cover_letter_path, answers_json=excluded.answers_json,
            screenshot_path=excluded.screenshot_path, error_code=excluded.error_code,
            notes=excluded.notes,
            submitted_at=COALESCE(excluded.submitted_at, applications.submitted_at),
            updated_at=excluded.updated_at
        """,
        (
            app.job_id,
            app.profile_id,
            app.status,
            app.ats,
            app.resume_path,
            app.cover_letter_path,
            json.dumps(app.answers, ensure_ascii=False),
            app.screenshot_path,
            app.error_code,
            app.notes,
            app.created_at or now,
            app.submitted_at,
            now,
        ),
    )
    # Do not trust lastrowid after an UPSERT: SQLite may keep a stale value
    # from an earlier insert on this connection, which would break the events FK.
    row = conn.execute(
        "SELECT id FROM applications WHERE job_id=? AND profile_id=?", (app.job_id, app.profile_id)
    ).fetchone()
    conn.commit()
    return int(row["id"])


def update_application(conn: sqlite3.Connection, app_id: int, **fields: Any) -> None:
    allowed = {
        "status",
        "ats",
        "resume_path",
        "cover_letter_path",
        "answers",
        "screenshot_path",
        "error_code",
        "notes",
        "submitted_at",
    }
    sets: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "answers":
            value = json.dumps(value, ensure_ascii=False)
            sets.append("answers_json=?")
        else:
            sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return
    sets.append("updated_at=?")
    values.append(utcnow())
    values.append(app_id)
    conn.execute(f"UPDATE applications SET {', '.join(sets)} WHERE id=?", values)
    conn.commit()


def _application_from_row(row: sqlite3.Row) -> Application:
    return Application(
        id=row["id"],
        job_id=row["job_id"],
        profile_id=row["profile_id"],
        status=row["status"],
        ats=row["ats"],
        resume_path=row["resume_path"],
        cover_letter_path=row["cover_letter_path"],
        answers=json.loads(row["answers_json"] or "{}"),
        screenshot_path=row["screenshot_path"],
        error_code=row["error_code"],
        notes=row["notes"],
        created_at=row["created_at"],
        submitted_at=row["submitted_at"],
        updated_at=row["updated_at"],
    )


def get_application(conn: sqlite3.Connection, app_id: int) -> Application | None:
    row = conn.execute("SELECT * FROM applications WHERE id=?", (app_id,)).fetchone()
    return _application_from_row(row) if row else None


def list_applications(
    conn: sqlite3.Connection, profile_id: int | None = None, status: str = "", limit: int = 200
) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: list[Any] = []
    if profile_id is not None:
        where.append("a.profile_id=?")
        params.append(profile_id)
    if status:
        where.append("a.status=?")
        params.append(status)
    rows = conn.execute(
        f"""
        SELECT a.*, j.title, j.company, j.location, j.apply_url, j.url
        FROM applications a JOIN jobs j ON j.id=a.job_id
        WHERE {' AND '.join(where)}
        ORDER BY a.updated_at DESC LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["answers"] = json.loads(d.pop("answers_json") or "{}")
        out.append(d)
    return out


def log_event(conn: sqlite3.Connection, application_id: int | None, kind: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO events (application_id, at, kind, detail) VALUES (?,?,?,?)",
        (application_id, utcnow(), kind, detail),
    )
    conn.commit()


def list_events(conn: sqlite3.Connection, application_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM events WHERE application_id=? ORDER BY at", (application_id,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, kind: str) -> int:
    cur = conn.execute(
        "INSERT INTO runs (kind, started_at, status) VALUES (?,?,?)",
        (kind, utcnow(), RunStatus.RUNNING.value),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, stats: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, stats_json=? WHERE id=?",
        (utcnow(), status, json.dumps(stats, ensure_ascii=False), run_id),
    )
    conn.commit()


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["stats"] = json.loads(d.pop("stats_json") or "{}")
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


def save_email(conn: sqlite3.Connection, msg: EmailMessage) -> int:
    if not msg.message_id:
        import hashlib

        basis = f"{msg.sender}|{msg.subject}|{msg.received_at}".encode("utf-8")
        msg.message_id = "local:" + hashlib.sha256(basis).hexdigest()[:32]
    cur = conn.execute(
        """
        INSERT INTO emails (application_id, message_id, sender, subject, body,
                            received_at, category, verification_code, forwarded)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(message_id) DO UPDATE SET
            application_id=COALESCE(excluded.application_id, emails.application_id),
            category=excluded.category, verification_code=excluded.verification_code
        """,
        (
            msg.application_id,
            msg.message_id,
            msg.sender,
            msg.subject,
            msg.body,
            msg.received_at,
            msg.category,
            msg.verification_code,
            int(msg.forwarded),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM emails WHERE message_id=?", (msg.message_id,)).fetchone()
    return int(row["id"]) if row else 0


def list_emails(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM emails ORDER BY received_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Saved / hidden / settings
# ---------------------------------------------------------------------------


def set_saved(conn: sqlite3.Connection, job_id: int, profile_id: int, saved: bool) -> None:
    if saved:
        conn.execute(
            "INSERT OR REPLACE INTO saved_jobs (job_id, profile_id, saved_at) VALUES (?,?,?)",
            (job_id, profile_id, utcnow()),
        )
    else:
        conn.execute("DELETE FROM saved_jobs WHERE job_id=? AND profile_id=?", (job_id, profile_id))
    conn.commit()


def set_hidden(conn: sqlite3.Connection, job_id: int, profile_id: int, hidden: bool) -> None:
    if hidden:
        conn.execute(
            "INSERT OR REPLACE INTO hidden_jobs (job_id, profile_id, hidden_at) VALUES (?,?,?)",
            (job_id, profile_id, utcnow()),
        )
    else:
        conn.execute("DELETE FROM hidden_jobs WHERE job_id=? AND profile_id=?", (job_id, profile_id))
    conn.commit()


def setting_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def setting_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()

# Agent threads and messages

def create_agent_thread(
    conn: sqlite3.Connection,
    profile_id: int,
    *,
    title: str = "New conversation",
    provider: str = "",
    model: str = "",
) -> int:
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO agent_threads (profile_id, title, provider, model, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (profile_id, title[:120] or "New conversation", provider, model, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_agent_threads(
    conn: sqlite3.Connection, profile_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM agent_threads WHERE profile_id=? ORDER BY updated_at DESC, id DESC LIMIT ?",
        (profile_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def get_agent_thread(conn: sqlite3.Connection, thread_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM agent_threads WHERE id=?", (thread_id,)).fetchone()
    return dict(row) if row else None


def update_agent_thread(
    conn: sqlite3.Connection,
    thread_id: int,
    *,
    title: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> None:
    fields = ["updated_at=?"]
    params: list[Any] = [utcnow()]
    if title is not None:
        fields.append("title=?")
        params.append(title[:120] or "New conversation")
    if provider is not None:
        fields.append("provider=?")
        params.append(provider)
    if model is not None:
        fields.append("model=?")
        params.append(model)
    params.append(thread_id)
    conn.execute(f"UPDATE agent_threads SET {', '.join(fields)} WHERE id=?", params)
    conn.commit()


def delete_agent_thread(conn: sqlite3.Connection, thread_id: int) -> None:
    conn.execute("DELETE FROM agent_threads WHERE id=?", (thread_id,))
    conn.commit()


def add_agent_message(
    conn: sqlite3.Connection,
    thread_id: int,
    role: str,
    *,
    content: str = "",
    name: str = "",
    tool_call_id: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, Any] | None = None,
) -> int:
    if role not in {"user", "assistant", "tool", "system"}:
        raise ValueError(f"Invalid agent message role: {role!r}")
    now = utcnow()
    cur = conn.execute(
        "INSERT INTO agent_messages "
        "(thread_id, role, content, name, tool_call_id, tool_calls_json, usage_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            thread_id,
            role,
            content,
            name,
            tool_call_id,
            json.dumps(tool_calls or [], ensure_ascii=False),
            json.dumps(usage or {}, ensure_ascii=False),
            now,
        ),
    )
    conn.execute("UPDATE agent_threads SET updated_at=? WHERE id=?", (now, thread_id))
    conn.commit()
    return int(cur.lastrowid)


def list_agent_messages(
    conn: sqlite3.Connection, thread_id: int, limit: int = 200
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM agent_messages WHERE thread_id=? ORDER BY id ASC LIMIT ?",
        (thread_id, limit),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["tool_calls"] = json.loads(data.pop("tool_calls_json") or "[]")
        data["usage"] = json.loads(data.pop("usage_json") or "{}")
        out.append(data)
    return out


# ---------------------------------------------------------------------------
# Connections and imported GitHub context

def save_connection(
    conn: sqlite3.Connection,
    provider: str,
    *,
    account_id: str = "",
    login: str = "",
    display_name: str = "",
    scopes: str = "",
    token_backend: str = "",
    meta: dict[str, Any] | None = None,
) -> None:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO connections
            (provider, account_id, login, display_name, scopes, token_backend,
             connected_at, last_sync_at, meta_json)
        VALUES (?,?,?,?,?,?,?,NULL,?)
        ON CONFLICT(provider) DO UPDATE SET
            account_id=excluded.account_id,
            login=excluded.login,
            display_name=excluded.display_name,
            scopes=excluded.scopes,
            token_backend=excluded.token_backend,
            connected_at=excluded.connected_at,
            meta_json=excluded.meta_json
        """,
        (
            provider,
            account_id,
            login,
            display_name,
            scopes,
            token_backend,
            now,
            json.dumps(meta or {}, ensure_ascii=False),
        ),
    )
    conn.commit()

def get_connection(conn: sqlite3.Connection, provider: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM connections WHERE provider=?", (provider,)).fetchone()
    if not row:
        return None
    data = dict(row)
    data["meta"] = json.loads(data.pop("meta_json") or "{}")
    return data

def touch_connection_sync(conn: sqlite3.Connection, provider: str) -> None:
    conn.execute(
        "UPDATE connections SET last_sync_at=? WHERE provider=?", (utcnow(), provider)
    )
    conn.commit()

def delete_connection(conn: sqlite3.Connection, provider: str) -> None:
    conn.execute("DELETE FROM connections WHERE provider=?", (provider,))
    conn.commit()

def _github_repo_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["languages"] = json.loads(data.pop("languages_json") or "{}")
    data["topics"] = json.loads(data.pop("topics_json") or "[]")
    data["tags"] = json.loads(data.pop("tags_json") or "[]")
    data["suggested_bullets"] = json.loads(data.pop("suggested_bullets_json") or "[]")
    data["private"] = bool(data.get("private"))
    data["fork"] = bool(data.get("fork"))
    data["archived"] = bool(data.get("archived"))
    data["ignored"] = bool(data.get("ignored"))
    return data

def upsert_github_repos(conn: sqlite3.Connection, repos: Iterable[dict[str, Any]]) -> int:
    """Upsert repository metadata; preserve cached README context on resync."""
    count = 0
    now = utcnow()
    for repo in repos:
        repo_id = int(repo.get("repo_id") or repo.get("id") or 0)
        if not repo_id:
            continue
        conn.execute(
            """
            INSERT INTO github_repos
                (repo_id, owner, full_name, name, html_url, description, homepage,
                 language, languages_json, topics_json, stars, forks, private, fork,
                 archived, pushed_at, created_at, readme, tech_line, tags_json,
                 suggested_bullets_json, ignored, synced_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)
            ON CONFLICT(repo_id) DO UPDATE SET
                owner=excluded.owner,
                full_name=excluded.full_name,
                name=excluded.name,
                html_url=excluded.html_url,
                description=excluded.description,
                homepage=excluded.homepage,
                language=excluded.language,
                languages_json=CASE WHEN excluded.languages_json='{}'
                    THEN github_repos.languages_json ELSE excluded.languages_json END,
                topics_json=excluded.topics_json,
                stars=excluded.stars,
                forks=excluded.forks,
                private=excluded.private,
                fork=excluded.fork,
                archived=excluded.archived,
                pushed_at=excluded.pushed_at,
                created_at=excluded.created_at,
                readme=CASE WHEN excluded.readme='' THEN github_repos.readme ELSE excluded.readme END,
                tech_line=CASE WHEN github_repos.tech_line='' THEN excluded.tech_line ELSE github_repos.tech_line END,
                tags_json=CASE WHEN excluded.tags_json='[]' THEN github_repos.tags_json ELSE excluded.tags_json END,
                suggested_bullets_json=CASE WHEN excluded.suggested_bullets_json='[]'
                    THEN github_repos.suggested_bullets_json ELSE excluded.suggested_bullets_json END,
                synced_at=excluded.synced_at
            """,
            (
                repo_id,
                str(repo.get("owner") or ""),
                str(repo.get("full_name") or ""),
                str(repo.get("name") or ""),
                str(repo.get("html_url") or ""),
                str(repo.get("description") or ""),
                str(repo.get("homepage") or ""),
                str(repo.get("language") or ""),
                json.dumps(repo.get("languages") or {}, ensure_ascii=False),
                json.dumps(repo.get("topics") or [], ensure_ascii=False),
                int(repo.get("stars") or 0),
                int(repo.get("forks") or 0),
                int(bool(repo.get("private"))),
                int(bool(repo.get("fork"))),
                int(bool(repo.get("archived"))),
                str(repo.get("pushed_at") or ""),
                str(repo.get("created_at") or ""),
                str(repo.get("readme") or ""),
                str(repo.get("tech_line") or ""),
                json.dumps(repo.get("tags") or [], ensure_ascii=False),
                json.dumps(repo.get("suggested_bullets") or [], ensure_ascii=False),
                now,
            ),
        )
        count += 1
    conn.commit()
    return count

def list_github_repos(
    conn: sqlite3.Connection,
    q: str = "",
    *,
    include_ignored: bool = False,
    limit: int = 300,
) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: list[Any] = []
    if not include_ignored:
        where.append("ignored=0")
    if q.strip():
        like = f"%{q.strip()}%"
        where.append("(full_name LIKE ? OR description LIKE ? OR language LIKE ? OR topics_json LIKE ?)")
        params.extend([like, like, like, like])
    rows = conn.execute(
        f"SELECT * FROM github_repos WHERE {' AND '.join(where)} "
        "ORDER BY archived ASC, pushed_at DESC, full_name ASC LIMIT ?",
        [*params, limit],
    ).fetchall()
    return [_github_repo_from_row(row) for row in rows]

def get_github_repo(conn: sqlite3.Connection, repo_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM github_repos WHERE repo_id=?", (repo_id,)).fetchone()
    return _github_repo_from_row(row) if row else None

def update_github_repo_context(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    readme: str,
    languages: dict[str, Any] | None = None,
    tech_line: str = "",
    tags: list[str] | None = None,
    suggested_bullets: list[str] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE github_repos SET readme=?, languages_json=?, tech_line=?, tags_json=?,
            suggested_bullets_json=?, synced_at=?
        WHERE repo_id=?
        """,
        (
            readme,
            json.dumps(languages or {}, ensure_ascii=False),
            tech_line,
            json.dumps(tags or [], ensure_ascii=False),
            json.dumps(suggested_bullets or [], ensure_ascii=False),
            utcnow(),
            repo_id,
        ),
    )
    conn.commit()

def set_github_repo_ignored(conn: sqlite3.Connection, repo_id: int, ignored: bool) -> None:
    conn.execute("UPDATE github_repos SET ignored=? WHERE repo_id=?", (int(ignored), repo_id))
    conn.commit()

def clear_github_repos(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM github_repos")
    conn.commit()

def count_github_repos(conn: sqlite3.Connection, *, include_ignored: bool = False) -> int:
    where = "" if include_ignored else " WHERE ignored=0"
    row = conn.execute(f"SELECT COUNT(*) AS n FROM github_repos{where}").fetchone()
    return int(row["n"] if row else 0)

# Stats / export
# ---------------------------------------------------------------------------


def stats(conn: sqlite3.Connection, profile_id: int | None = None) -> dict[str, Any]:
    job_count = count_jobs(conn, profile_id)
    profile_filter = "WHERE profile_id=?" if profile_id is not None else ""
    params: tuple[Any, ...] = (profile_id,) if profile_id is not None else ()
    app_rows = conn.execute(
        f"SELECT status, COUNT(*) AS n FROM applications {profile_filter} GROUP BY status", params
    ).fetchall()
    apps = {r["status"]: r["n"] for r in app_rows}
    avg = conn.execute(
        "SELECT AVG(m.score) AS s FROM matches m JOIN jobs j ON j.id=m.job_id "
        + ("WHERE m.profile_id=? AND " if profile_id is not None else "WHERE ")
        + "j.source <> 'resume-forge'",
        params,
    ).fetchone()
    return {
        "jobs": job_count,
        "applications": apps,
        "applications_total": sum(apps.values()),
        "avg_match": round(float(avg["s"] or 0), 1),
    }


def _agent_instance_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return dict(row)


def create_agent_instance(conn: sqlite3.Connection, label: str = "local") -> dict[str, Any]:
    instance_id = f"inst_{uuid.uuid4().hex}"
    now = utcnow()
    conn.execute(
        "INSERT INTO agent_instances (id, label, created_at, last_seen_at) VALUES (?,?,?,?)",
        (instance_id, label[:80], now, now),
    )
    conn.commit()
    return get_agent_instance(conn, instance_id) or {}


def get_agent_instance(conn: sqlite3.Connection, instance_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM agent_instances WHERE id=?", (instance_id,)).fetchone()
    return _agent_instance_from_row(row) if row else None


def list_agent_instances(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM agent_instances ORDER BY created_at, id").fetchall()
    return [_agent_instance_from_row(row) for row in rows]


def _agent_connection_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["scopes"] = json.loads(data.pop("scopes_json") or "[]")
    data["enabled"] = bool(data["enabled"])
    return data


def create_agent_connection(
    conn: sqlite3.Connection,
    *,
    instance_id: str,
    profile_id: int,
    client: str = "generic",
    label: str = "",
    scopes: list[str] | None = None,
    enabled: bool = True,
) -> dict[str, Any]:
    if get_agent_instance(conn, instance_id) is None:
        raise ValueError("Agent instance not found")
    if get_profile(conn, profile_id) is None:
        raise ValueError("Profile not found")
    connection_id = f"conn_{uuid.uuid4().hex}"
    now = utcnow()
    normalized = sorted({str(item).strip() for item in (scopes or []) if str(item).strip()})
    conn.execute(
        """
        INSERT INTO agent_connections
            (id, instance_id, profile_id, client, label, scopes_json, enabled,
             generation, created_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (
            connection_id,
            instance_id,
            profile_id,
            client[:40],
            label[:120],
            json.dumps(normalized, ensure_ascii=False),
            int(bool(enabled)),
            1,
            now,
        ),
    )
    conn.commit()
    return get_agent_connection(conn, connection_id) or {}


def get_agent_connection(conn: sqlite3.Connection, connection_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM agent_connections WHERE id=?", (connection_id,)).fetchone()
    return _agent_connection_from_row(row) if row else None


def list_agent_connections(
    conn: sqlite3.Connection, *, include_disabled: bool = True
) -> list[dict[str, Any]]:
    where = "" if include_disabled else "WHERE enabled=1"
    rows = conn.execute(
        f"SELECT * FROM agent_connections {where} ORDER BY created_at, id"
    ).fetchall()
    return [_agent_connection_from_row(row) for row in rows]


def update_agent_connection_scopes(
    conn: sqlite3.Connection, connection_id: str, scopes: list[str]
) -> dict[str, Any] | None:
    current = get_agent_connection(conn, connection_id)
    if current is None:
        return None
    merged = sorted(set(current.get("scopes") or []) | {str(item).strip() for item in scopes if str(item).strip()})
    conn.execute(
        "UPDATE agent_connections SET scopes_json=?, generation=generation+1 WHERE id=?",
        (json.dumps(merged, ensure_ascii=False), connection_id),
    )
    conn.commit()
    return get_agent_connection(conn, connection_id)


def disable_agent_connection(conn: sqlite3.Connection, connection_id: str) -> dict[str, Any] | None:
    now = utcnow()
    conn.execute(
        "UPDATE agent_connections SET enabled=0, revoked_at=?, generation=generation+1 "
        "WHERE id=?",
        (now, connection_id),
    )
    conn.commit()
    return get_agent_connection(conn, connection_id)


def touch_agent_connection(conn: sqlite3.Connection, connection_id: str) -> None:
    conn.execute(
        "UPDATE agent_connections SET last_used_at=? WHERE id=?",
        (utcnow(), connection_id),
    )
    conn.commit()


def remember_agent_memory(
    conn: sqlite3.Connection,
    *,
    connection_id: str,
    profile_id: int,
    key: str,
    value: str,
) -> dict[str, Any]:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO agent_memories (connection_id, profile_id, key, value, created_at, updated_at)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(profile_id, key) DO UPDATE SET
            connection_id=excluded.connection_id,
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (connection_id, profile_id, key, value, now, now),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM agent_memories WHERE profile_id=? AND key=?",
        (profile_id, key),
    ).fetchone()
    return dict(row) if row else {}


def list_agent_memories(
    conn: sqlite3.Connection,
    *,
    profile_id: int,
    query: str = "",
    limit: int = 20,
) -> list[dict[str, Any]]:
    where = ["profile_id=?"]
    params: list[Any] = [profile_id]
    if query:
        where.append("(key LIKE ? OR value LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])
    rows = conn.execute(
        f"SELECT * FROM agent_memories WHERE {' AND '.join(where)} "
        "ORDER BY updated_at DESC, id DESC LIMIT ?",
        [*params, max(1, min(50, limit))],
    ).fetchall()
    return [dict(row) for row in rows]


def forget_agent_memory(conn: sqlite3.Connection, *, profile_id: int, key: str) -> bool:
    cur = conn.execute(
        "DELETE FROM agent_memories WHERE profile_id=? AND key=?",
        (profile_id, key),
    )
    conn.commit()
    return cur.rowcount > 0


def _operation_from_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json") or "{}")
    data["result"] = json.loads(data.pop("result_json") or "{}")
    return data


def create_agent_operation(
    conn: sqlite3.Connection,
    *,
    connection_id: str,
    profile_id: int,
    action: str,
    payload: dict[str, Any],
    idempotency_key: str,
    ttl_minutes: int = 1440,
) -> dict[str, Any]:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    payload_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT * FROM agent_operations WHERE connection_id=? AND idempotency_key=?",
        (connection_id, idempotency_key),
    ).fetchone()
    if existing:
        if existing["payload_hash"] != payload_hash:
            raise ValueError("Idempotency key already used with a different request.")
        return _operation_from_row(existing)
    now = utcnow()
    expires = datetime.now(timezone.utc) + timedelta(minutes=max(1, min(1440, ttl_minutes)))
    operation_id = f"op_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO agent_operations
            (id, connection_id, profile_id, action, payload_json, payload_hash,
             idempotency_key, state, created_at, expires_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        (
            operation_id,
            connection_id,
            profile_id,
            action,
            canonical,
            payload_hash,
            idempotency_key,
            "pending_approval",
            now,
            expires.isoformat(timespec="seconds"),
        ),
    )
    append_agent_operation_event(conn, operation_id, "created", {"action": action})
    conn.commit()
    return get_agent_operation(conn, operation_id) or {}


def get_agent_operation(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM agent_operations WHERE id=?", (operation_id,)
    ).fetchone()
    return _operation_from_row(row) if row else None


def list_agent_operations(
    conn: sqlite3.Connection,
    *,
    profile_id: int | None = None,
    connection_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    where = ["1=1"]
    params: list[Any] = []
    if profile_id is not None:
        where.append("profile_id=?")
        params.append(profile_id)
    if connection_id is not None:
        where.append("connection_id=?")
        params.append(connection_id)
    rows = conn.execute(
        f"SELECT * FROM agent_operations WHERE {' AND '.join(where)} "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        [*params, max(1, min(100, limit))],
    ).fetchall()
    return [_operation_from_row(row) for row in rows]


def append_agent_operation_event(
    conn: sqlite3.Connection, operation_id: str, kind: str, detail: dict[str, Any] | None = None
) -> None:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq),0)+1 AS next FROM agent_operation_events WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    conn.execute(
        "INSERT INTO agent_operation_events (operation_id, seq, at, kind, detail_json) "
        "VALUES (?,?,?,?,?)",
        (
            operation_id,
            int(row["next"]),
            utcnow(),
            kind,
            json.dumps(detail or {}, ensure_ascii=False),
        ),
    )
    conn.commit()


def update_agent_operation(conn: sqlite3.Connection, operation_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {
        "state",
        "approved_at",
        "started_at",
        "finished_at",
        "owner_nonce",
        "result_json",
        "error_code",
        "error_message",
    }
    sets: list[str] = []
    values: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "result_json" and not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        sets.append(f"{key}=?")
        values.append(value)
    if not sets:
        return get_agent_operation(conn, operation_id)
    values.append(operation_id)
    conn.execute(f"UPDATE agent_operations SET {', '.join(sets)} WHERE id=?", values)
    conn.commit()
    return get_agent_operation(conn, operation_id)


def approve_agent_operation(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
    now = utcnow()
    conn.execute(
        "UPDATE agent_operations SET state='approved', approved_at=? "
        "WHERE id=? AND state='pending_approval'",
        (now, operation_id),
    )
    conn.commit()
    operation = get_agent_operation(conn, operation_id)
    if operation and operation["state"] == "approved":
        append_agent_operation_event(conn, operation_id, "approved", {})
    return operation


def deny_agent_operation(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
    conn.execute(
        "UPDATE agent_operations SET state='denied', finished_at=? "
        "WHERE id=? AND state='pending_approval'",
        (utcnow(), operation_id),
    )
    conn.commit()
    operation = get_agent_operation(conn, operation_id)
    if operation and operation["state"] == "denied":
        append_agent_operation_event(conn, operation_id, "denied", {})
    return operation


def claim_agent_operation(
    conn: sqlite3.Connection, operation_id: str, owner_nonce: str
) -> dict[str, Any] | None:
    now = utcnow()
    cur = conn.execute(
        "UPDATE agent_operations SET state='running', started_at=?, owner_nonce=? "
        "WHERE id=? AND state='approved'",
        (now, owner_nonce, operation_id),
    )
    conn.commit()
    if cur.rowcount != 1:
        return get_agent_operation(conn, operation_id)
    append_agent_operation_event(conn, operation_id, "claimed", {"owner": owner_nonce[:12]})
    return get_agent_operation(conn, operation_id)


def export_all(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = [
        "jobs",
        "profiles",
        "preferences",
        "matches",
        "applications",
        "events",
        "runs",
        "emails",
        "saved_jobs",
        "hidden_jobs",
        "settings",
        "connections",
        "github_repos",
        "agent_threads",
        "agent_messages",
        "agent_instances",
        "agent_connections",
        "agent_memories",
        "agent_operations",
        "agent_operation_events",
    ]
    out: dict[str, Any] = {}
    for table in tables:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        out[table] = [dict(r) for r in rows]
    # Worker claim nonces are authority material, not user data.
    for row in out.get("agent_operations", []):
        row.pop("owner_nonce", None)
    return out


def delete_all(conn: sqlite3.Connection) -> None:
    for table in [
        "events",
        "applications",
        "matches",
        "emails",
        "saved_jobs",
        "hidden_jobs",
        "preferences",
        "profiles",
        "jobs",
        "runs",
        "settings",
        "connections",
        "github_repos",
        "agent_messages",
        "agent_threads",
        "agent_operation_events",
        "agent_operations",
        "agent_memories",
        "agent_connections",
        "agent_instances",
    ]:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
