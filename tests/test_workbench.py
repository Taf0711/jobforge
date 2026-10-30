from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from jobforge import cli, db
from jobforge.config import Config, BrowserConfig, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Application, Job, MatchResult, Preferences, Profile
from jobforge.workbench.authorization import load_query_context
from jobforge.workbench.contracts import (
    ToolError,
    parse_json_strict,
    validate_tool_input,
)
from jobforge.workbench.queries import call_tool, tool_list


def _config(tmp_path: Path) -> Config:
    cfg = Config(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        db_path=tmp_path / "data" / "test.sqlite3",
        profile_path=None,
        resume_path=None,
        sources={},
        preferences={},
        llm=LLMConfig(),
        email=EmailConfig(),
        browser=BrowserConfig(),
        server=ServerConfig(),
    )
    cfg.ensure_dirs()
    db.init_db(cfg.db_path)
    return cfg


def _seed(tmp_path: Path) -> tuple[Config, int, int, int]:
    cfg = _config(tmp_path)
    conn = db.connect(cfg.db_path)
    profile = Profile(
        name="Workbench Test",
        first_name="Test",
        last_name="User",
        email="private@example.com",
        phone="+1 555 010 0000",
        headline="Backend engineer",
        location="New York, NY",
        resume_path=str(tmp_path / "resume.pdf"),
        skills=["Python", "Rust", "PostgreSQL"],
        experience=[
            {
                "title": "Software Engineer",
                "company": "Acme",
                "location": "New York, NY",
                "start": "2024-01",
                "end": "2025-01",
                "bullets": [
                    "Contact private@example.com or +1 555 019 9000. Built Python services.",
                    "Reduced latency 30%.",
                ],
            }
        ],
        projects=[
            {
                "title": "Alpha",
                "company": "",
                "tech": "Rust",
                "url": "https://example.com/alpha",
                "bullets": ["Built a Rust cache."],
            }
        ],
        facts=[
            {
                "id": "f1",
                "section": "experience",
                "title": "Software Engineer",
                "company": "Acme",
                "text": "Built Python services.",
                "skills": ["Python"],
            }
        ],
    )
    profile_id = db.save_profile(conn, profile)
    db.set_active_profile(conn, profile_id)
    db.save_preferences(conn, profile_id, Preferences(titles=["Backend Engineer"], min_match=55))
    job_ids = []
    for index, score in enumerate((88, 66, 20), start=1):
        job_id = db.upsert_job(
            conn,
            Job(
                source="greenhouse",
                source_id=f"workbench-{index}",
                company=f"Company {index}",
                title=f"Backend Engineer {index}",
                location="New York, NY",
                url=f"https://example.com/jobs/{index}",
                apply_url=f"https://example.com/jobs/{index}/apply",
                description=f"Rust and Python platform role {index}. " + ("x" * 20_000),
            ),
        )
        db.set_match(
            conn,
            MatchResult(
                job_id=job_id,
                profile_id=profile_id,
                score=float(score),
                coverage=0.8,
                gate_survival=1.0,
                reasons=["Strong skill coverage"],
                gaps=["Nice-to-have: Kubernetes"],
                requirements=[
                    {
                        "requirement": "Rust",
                        "skill": "Rust",
                        "importance": "must",
                        "credit": 1.0,
                        "weight": 1.0,
                        "matched_by": "skill",
                        "gap": False,
                    }
                ],
                meta={"pursuit": 70.0},
            ),
        )
        job_ids.append(job_id)
    app_id = db.create_application(
        conn,
        Application(
            job_id=job_ids[0],
            profile_id=profile_id,
            status="prepared",
            ats="greenhouse",
            resume_path=str(tmp_path / "secret-resume.pdf"),
            answers={"salary": "150000", "authorized": "yes"},
            screenshot_path=str(tmp_path / "secret.png"),
            notes="private note",
        ),
    )
    instance = db.create_agent_instance(conn, "local-test")
    connection = db.create_agent_connection(
        conn,
        instance_id=str(instance["id"]),
        profile_id=profile_id,
        client="generic",
        label="test connection",
        scopes=["jobs:read", "matches:read", "profile:resume_context", "applications:summary"],
    )
    conn.close()
    return cfg, profile_id, job_ids[0], app_id, str(instance["id"]), str(connection["id"])


def _context(cfg: Config, instance_id: str, connection_id: str):
    conn = db.connect(cfg.db_path)
    return conn, load_query_context(
        cfg, conn, connection_id=connection_id, instance_id=instance_id
    )


def test_contracts_reject_duplicate_keys_bools_and_unknown_fields():
    with pytest.raises(ToolError) as duplicate:
        parse_json_strict('{"a":1,"a":2}')
    assert duplicate.value.code == "INVALID_ARGUMENT"

    with pytest.raises(ToolError):
        parse_json_strict('{"a":NaN}')

    with pytest.raises(ToolError) as boolean_limit:
        validate_tool_input("jobforge_search_jobs", {"limit": True})
    assert boolean_limit.value.code == "INVALID_ARGUMENT"

    with pytest.raises(ToolError) as extra:
        validate_tool_input("jobforge_status", {"unexpected": 1})
    assert extra.value.code == "INVALID_ARGUMENT"

    assert validate_tool_input("jobforge_search_jobs", {})["limit"] == 10


def test_connection_requires_enabled_matching_instance_and_scope(tmp_path: Path):
    cfg, _profile_id, _job_id, _app_id, instance_id, connection_id = _seed(tmp_path)
    conn, context = _context(cfg, instance_id, connection_id)
    try:
        assert "jobs:read" in context.scopes
        with pytest.raises(ToolError) as wrong_instance:
            load_query_context(
                cfg, conn, connection_id=connection_id, instance_id="inst_wrong"
            )
        assert wrong_instance.value.code == "NOT_FOUND"
    finally:
        conn.close()

    conn = db.connect(cfg.db_path)
    narrow = db.create_agent_connection(
        conn,
        instance_id=instance_id,
        profile_id=_profile_id,
        client="generic",
        label="jobs only",
        scopes=["jobs:read"],
    )
    conn.close()
    conn, context = _context(cfg, instance_id, str(narrow["id"]))
    try:
        payload = call_tool(context, "jobforge_get_profile", {})
        assert payload["ok"] is False
        assert payload["error"]["code"] == "SCOPE_DENIED"
        assert call_tool(context, "jobforge_search_jobs", {"limit": 1})["ok"] is True
    finally:
        conn.close()

    conn = db.connect(cfg.db_path)
    disabled = db.create_agent_connection(
        conn,
        instance_id=instance_id,
        profile_id=_profile_id,
        client="generic",
        label="disabled",
        scopes=["jobs:read"],
        enabled=False,
    )
    conn.close()
    with pytest.raises(ToolError) as blocked:
        conn = db.connect(cfg.db_path)
        try:
            load_query_context(cfg, conn, connection_id=str(disabled["id"]))
        finally:
            conn.close()
    assert blocked.value.code == "CONNECTION_DISABLED"


def test_search_jobs_is_bounded_and_cursor_is_revision_bound(tmp_path: Path):
    cfg, _profile_id, _job_id, _app_id, instance_id, connection_id = _seed(tmp_path)
    conn, context = _context(cfg, instance_id, connection_id)
    try:
        first = call_tool(context, "jobforge_search_jobs", {"limit": 2})
        assert first["ok"] is True
        payload = first["data"]
        assert len(payload["jobs"]) == 2
        assert payload["jobs"][0]["job_id"] > 0
        assert "description" not in payload["jobs"][0]
        assert payload["jobs"][0]["fit"] == 88.0
        assert payload["next_cursor"]
        cursor = payload["next_cursor"]

        second = call_tool(
            context, "jobforge_search_jobs", {"limit": 2, "cursor": cursor}
        )
        assert second["ok"] is True
        assert second["data"]["jobs"]
        assert second["data"]["jobs"][0]["job_id"] not in {
            item["job_id"] for item in payload["jobs"]
        }

        filtered = call_tool(context, "jobforge_search_jobs", {"min_fit": 80})
        assert [item["fit"] for item in filtered["data"]["jobs"]] == [88.0]

        # A changed dataset invalidates the old cursor.
        conn2 = db.connect(cfg.db_path)
        db.upsert_job(
            conn2,
            Job(
                source="greenhouse",
                source_id="new-job",
                company="New Co",
                title="New Role",
                description="New role",
            ),
        )
        conn2.close()
        stale = call_tool(
            context, "jobforge_search_jobs", {"limit": 2, "cursor": cursor}
        )
        assert stale["ok"] is False
        assert stale["error"]["code"] == "STALE_CONTEXT"

        bad = call_tool(context, "jobforge_search_jobs", {"limit": 2, "cursor": "not-a-cursor"})
        assert bad["ok"] is False
        assert bad["error"]["code"] == "INVALID_ARGUMENT"
    finally:
        conn.close()


def test_get_job_redacts_unsafe_urls_and_marks_description_untrusted(tmp_path: Path):
    cfg, _profile_id, job_id, _app_id, instance_id, connection_id = _seed(tmp_path)
    conn, context = _context(cfg, instance_id, connection_id)
    try:
        payload = call_tool(
            context, "jobforge_get_job", {"job_id": job_id, "include_description": True}
        )
        assert payload["ok"] is True
        data = payload["data"]
        assert data["description_untrusted"] is True
        assert data["description_truncated"] is True
        assert len(data["description"]) == 12_000
        # The stored HTTPS URL is preserved; credential-bearing/scheme values
        # are rejected by the projection helper.
        assert data["url"].startswith("https://")
    finally:
        conn.close()

    conn = db.connect(cfg.db_path)
    db.upsert_job(
        conn,
        Job(
            source="lever",
            source_id="unsafe-url",
            company="Unsafe",
            title="Unsafe",
            url="javascript:alert(1)",
            apply_url="https://user:pass@example.com/apply",
        ),
    )
    row = conn.execute(
        "SELECT id FROM jobs WHERE source_id='unsafe-url'"
    ).fetchone()
    conn.close()
    conn, context = _context(cfg, instance_id, connection_id)
    try:
        data = call_tool(context, "jobforge_get_job", {"job_id": int(row["id"])})
        assert data["data"]["url"] == ""
        assert data["data"]["apply_url"] == ""
    finally:
        conn.close()


def test_profile_projection_redacts_contact_details(tmp_path: Path):
    cfg, _profile_id, _job_id, _app_id, instance_id, connection_id = _seed(tmp_path)
    conn, context = _context(cfg, instance_id, connection_id)
    try:
        payload = call_tool(context, "jobforge_get_profile", {"section": "experience"})
        assert payload["ok"] is True
        encoded = json.dumps(payload["data"])
        assert "private@example.com" not in encoded
        assert "+1 555 019 9000" not in encoded
        assert "[redacted email]" in encoded
        assert "[redacted phone]" in encoded
        assert "email" not in payload["data"]["items"][0]
        assert "phone" not in payload["data"]["items"][0]
    finally:
        conn.close()


def test_application_projection_excludes_answers_notes_and_paths(tmp_path: Path):
    cfg, _profile_id, _job_id, _app_id, instance_id, connection_id = _seed(tmp_path)
    conn, context = _context(cfg, instance_id, connection_id)
    try:
        payload = call_tool(context, "jobforge_list_applications", {"status": "prepared"})
        assert payload["ok"] is True
        encoded = json.dumps(payload["data"])
        assert "150000" not in encoded
        assert "private note" not in encoded
        assert "secret-resume.pdf" not in encoded
        assert "secret.png" not in encoded
        assert payload["data"]["applications"][0]["channel"] == "greenhouse"
    finally:
        conn.close()


def test_explain_fit_and_readiness_are_safe(tmp_path: Path):
    cfg, _profile_id, job_id, _app_id, instance_id, connection_id = _seed(tmp_path)
    conn, context = _context(cfg, instance_id, connection_id)
    try:
        fit = call_tool(context, "jobforge_explain_fit", {"job_id": job_id})
        assert fit["ok"] is True
        assert fit["data"]["fit"] == 88.0
        assert fit["data"]["requirements"][0]["matched_by"] == "skill"
        assert "private@example.com" not in json.dumps(fit["data"])

        readiness = call_tool(context, "jobforge_get_readiness", {"job_id": job_id})
        assert readiness["ok"] is True
        assert readiness["data"]["review_state"] == "not_tracked"
        assert "local path" not in json.dumps(readiness["data"])
        assert str(tmp_path) not in json.dumps(readiness["data"])
    finally:
        conn.close()


def test_tool_list_marks_scope_availability(tmp_path: Path):
    cfg, profile_id, _job_id, _app_id, instance_id, _connection_id = _seed(tmp_path)
    conn = db.connect(cfg.db_path)
    narrow = db.create_agent_connection(
        conn,
        instance_id=instance_id,
        profile_id=profile_id,
        client="generic",
        label="jobs only",
        scopes=["jobs:read"],
    )
    conn.close()
    conn, context = _context(cfg, instance_id, str(narrow["id"]))
    try:
        tools = {item["name"]: item for item in tool_list(context)}
        assert tools["jobforge_search_jobs"]["available"] is True
        assert tools["jobforge_get_profile"]["available"] is False
        assert tools["jobforge_status"]["annotations"]["readOnlyHint"] is True
    finally:
        conn.close()


def test_schema_v6_connections_export_and_delete(tmp_path: Path):
    cfg = _config(tmp_path)
    conn = db.connect(cfg.db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    profile_id = db.save_profile(conn, Profile(name="schema"))
    instance = db.create_agent_instance(conn, "local")
    connection = db.create_agent_connection(
        conn,
        instance_id=str(instance["id"]),
        profile_id=profile_id,
        scopes=["jobs:read"],
    )
    assert connection["enabled"] is True
    disabled = db.disable_agent_connection(conn, str(connection["id"]))
    assert disabled is not None
    assert disabled["enabled"] is False
    assert disabled["generation"] == 2
    assert "agent_connections" in db.export_all(conn)
    db.delete_all(conn)
    assert db.list_agent_connections(conn) == []
    conn.close()


def test_cli_setup_list_and_tools_call(tmp_path: Path, monkeypatch, capsys):
    cfg = _config(tmp_path)
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "data_dir": str(cfg.data_dir),
                "output_dir": str(cfg.output_dir),
                "features": {"agent_integrations": True},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    cli_db = cfg.data_dir / "jobforge.sqlite3"
    db.init_db(cli_db)
    conn = db.connect(cli_db)
    profile_id = db.save_profile(conn, Profile(name="CLI Test"))
    db.set_active_profile(conn, profile_id)
    job_id = db.upsert_job(
        conn,
        Job(
            source="greenhouse",
            source_id="cli-job",
            company="CLI Co",
            title="Backend Engineer",
            description="Python role",
        ),
    )
    db.set_match(
        conn,
        MatchResult(
            job_id=job_id,
            profile_id=profile_id,
            score=77.0,
            coverage=0.7,
            gate_survival=1.0,
        ),
    )
    conn.close()

    assert cli.main(
        [
            "--config",
            str(config_path),
            "integrations",
            "setup",
            "--profile",
            str(profile_id),
            "--client",
            "generic",
            "--scopes",
            "profile:resume_context,applications:summary",
        ]
    ) == 0
    setup_payload = json.loads(capsys.readouterr().out)
    connection_id = setup_payload["connection"]["id"]
    assert "profile:resume_context" in setup_payload["connection"]["scopes"]

    assert cli.main(["--config", str(config_path), "integrations", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["connections"][0]["id"] == connection_id

    assert cli.main(
        [
            "--config",
            str(config_path),
            "integrations",
            "config",
            "--connection",
            connection_id,
        ]
    ) == 0
    mcp_config = json.loads(capsys.readouterr().out)["mcpServers"]["jobforge"]
    assert mcp_config["command"]
    assert "mcp" in mcp_config["args"]
    assert connection_id in mcp_config["args"]

    monkeypatch.setattr("sys.stdin", io.StringIO('{"limit": 1}'))
    assert cli.main(
        [
            "--config",
            str(config_path),
            "tools",
            "call",
            "jobforge_search_jobs",
            "--connection",
            connection_id,
        ]
    ) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["data"]["jobs"][0]["job_id"] == job_id

    assert cli.main(
        [
            "--config",
            str(config_path),
            "tools",
            "list",
            "--connection",
            connection_id,
        ]
    ) == 0
    listed_tools = json.loads(capsys.readouterr().out)
    assert listed_tools["ok"] is True
    assert any(item["name"] == "jobforge_status" for item in listed_tools["data"])


def test_tools_call_does_not_initialize_a_missing_database(tmp_path: Path, capsys):
    cfg = _config(tmp_path)
    config_path = tmp_path / "config.yml"
    config_path.write_text(
        yaml.safe_dump({"data_dir": str(cfg.data_dir), "output_dir": str(cfg.output_dir)}),
        encoding="utf-8",
    )
    # Point the config at a data dir without a database.
    empty = tmp_path / "empty-data"
    empty.mkdir()
    config_path.write_text(
        yaml.safe_dump({"data_dir": str(empty), "output_dir": str(cfg.output_dir)}),
        encoding="utf-8",
    )
    assert cli.main(
        [
            "--config",
            str(config_path),
            "tools",
            "call",
            "jobforge_status",
            "--connection",
            "conn_missing",
        ]
    ) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "SETUP_REQUIRED"
    assert not (empty / "jobforge.sqlite3").exists()


def test_old_schema_returns_migration_required(tmp_path: Path):
    import sqlite3

    cfg = _config(tmp_path)
    raw = tmp_path / "old.sqlite3"
    sqlite3.connect(raw).close()
    conn = db.connect(raw)
    try:
        with pytest.raises(ToolError) as exc:
            load_query_context(cfg, conn, connection_id="conn_old")
    finally:
        conn.close()
    assert exc.value.code == "MIGRATION_REQUIRED"
