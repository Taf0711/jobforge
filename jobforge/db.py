"""SQLite persistence layer. Stdlib only, parameterized queries everywhere."""

from __future__ import annotations

import json
import sqlite3
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

SCHEMA_VERSION = 2

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
"""


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
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
        row = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs j LEFT JOIN hidden_jobs h "
            "ON h.job_id=j.id AND h.profile_id=? WHERE h.job_id IS NULL",
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
            notes=excluded.notes, updated_at=excluded.updated_at
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


# ---------------------------------------------------------------------------
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
        "SELECT AVG(score) AS s FROM matches " + ("WHERE profile_id=?" if profile_id is not None else ""),
        params,
    ).fetchone()
    return {
        "jobs": job_count,
        "applications": apps,
        "applications_total": sum(apps.values()),
        "avg_match": round(float(avg["s"] or 0), 1),
    }


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
    ]
    out: dict[str, Any] = {}
    for table in tables:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        out[table] = [dict(r) for r in rows]
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
    ]:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()
