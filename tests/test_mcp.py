from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import yaml

from jobforge import db
from jobforge.config import Config, BrowserConfig, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Job, MatchResult, Profile

pytest.importorskip("mcp")


def _seed(tmp_path: Path):
    cfg = Config(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        db_path=tmp_path / "data" / "jobforge.sqlite3",
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
    (tmp_path / "base.pdf").write_bytes(b"%PDF-1.4\n")
    db.init_db(cfg.db_path)
    conn = db.connect(cfg.db_path)
    profile_id = db.save_profile(
        conn,
        Profile(
            name="MCP Test",
            first_name="Test",
            last_name="User",
            email="private@example.com",
            resume_path=str(tmp_path / "base.pdf"),
            skills=["Python", "Rust"],
            experience=[
                {
                    "title": "Engineer",
                    "company": "Acme",
                    "bullets": ["Built Python services. private@example.com"],
                }
            ],
        ),
    )
    db.set_active_profile(conn, profile_id)
    job_id = db.upsert_job(
        conn,
        Job(
            source="greenhouse",
            source_id="mcp-job",
            company="MCP Co",
            title="Backend Engineer",
            description="Python platform role " + ("x" * 13_000),
        ),
    )
    db.set_match(
        conn,
        MatchResult(
            job_id=job_id,
            profile_id=profile_id,
            score=81.0,
            coverage=0.8,
            gate_survival=1.0,
        ),
    )
    instance = db.create_agent_instance(conn, "mcp-test")
    connection = db.create_agent_connection(
        conn,
        instance_id=str(instance["id"]),
        profile_id=profile_id,
        client="generic",
        label="mcp connection",
        scopes=["jobs:read", "matches:read"],
    )
    conn.close()
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
    return config_path, str(instance["id"]), str(connection["id"]), job_id


def test_mcp_stdio_lists_tools_and_serves_reads(tmp_path: Path):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    config_path, instance_id, connection_id, job_id = _seed(tmp_path)
    repo = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "jobforge",
            "--config",
            str(config_path),
            "mcp",
            "serve",
            "--connection",
            connection_id,
            "--instance",
            instance_id,
        ],
        cwd=str(repo),
    )

    async def exercise() -> None:
        async with Client(params) as client:
            listed = await client.list_tools()
            names = [tool.name for tool in listed.tools]
            assert names == [
                "jobforge_status",
                "jobforge_search_jobs",
                "jobforge_get_job",
                "jobforge_explain_fit",
                "jobforge_get_readiness",
                "jobforge_get_profile",
                "jobforge_list_applications",
                "jobforge_remember",
                "jobforge_recall",
                "jobforge_forget",
                "jobforge_dashboard",
                "jobforge_open_dashboard",
                "jobforge_request_resume",
                "jobforge_get_operation",
                "jobforge_list_operations",
            ]
            assert all(tool.annotations for tool in listed.tools)
            assert all(
                tool.annotations.read_only_hint
                for tool in listed.tools
                if tool.name
                in {
                    "jobforge_status",
                    "jobforge_search_jobs",
                    "jobforge_get_job",
                    "jobforge_explain_fit",
                    "jobforge_get_readiness",
                    "jobforge_get_profile",
                    "jobforge_list_applications",
                    "jobforge_recall",
                    "jobforge_dashboard",
                    "jobforge_get_operation",
                    "jobforge_list_operations",
                }
            )
            by_name = {tool.name: tool for tool in listed.tools}
            assert by_name["jobforge_remember"].annotations.read_only_hint is False
            assert by_name["jobforge_forget"].annotations.destructive_hint is True
            assert by_name["jobforge_status"].title == "JobForge status"
            assert by_name["jobforge_status"].output_schema["required"][0] == "schema_version"
            search_schema = by_name["jobforge_search_jobs"].input_schema
            limit_schema = search_schema["properties"]["limit"]["anyOf"][0]
            assert limit_schema["maximum"] == 50
            fit_schema = search_schema["properties"]["min_fit"]["anyOf"][0]
            assert fit_schema["minimum"] == 0
            assert fit_schema["maximum"] == 100

            status = await client.call_tool("jobforge_status", {})
            assert status.is_error is False
            assert status.structured_content["ok"] is True
            assert status.structured_content["data"]["profile"]["label"] == "MCP Test"
            assert status.structured_content["data"]["capabilities"]["read_only"] is True

            search = await client.call_tool("jobforge_search_jobs", {"limit": 1})
            assert search.is_error is False
            assert search.structured_content["data"]["jobs"][0]["job_id"] == job_id

            filtered = await client.call_tool("jobforge_search_jobs", {"min_fit": 80})
            assert filtered.is_error is False
            assert [item["fit"] for item in filtered.structured_content["data"]["jobs"]] == [81.0]

            profile = await client.call_tool("jobforge_get_profile", {})
            assert profile.is_error is True
            assert profile.structured_content["error"]["code"] == "SCOPE_DENIED"

            memory = await client.call_tool(
                "jobforge_remember", {"key": "preference", "value": "remote roles"}
            )
            assert memory.is_error is True
            assert memory.structured_content["error"]["code"] == "SCOPE_DENIED"

            dashboard = await client.call_tool("jobforge_dashboard", {})
            assert dashboard.is_error is False
            assert dashboard.structured_content["data"]["url"].startswith("http://")

            invalid = await client.call_tool("jobforge_search_jobs", {"limit": True})
            assert invalid.is_error is True
            # Strict schema validation happens before the tool body, so this
            # arrives as an MCP tool error rather than a JobForge envelope.
            assert "limit" in invalid.content[0].text

    asyncio.run(exercise())


def test_mcp_startup_fails_closed_for_a_disabled_connection(tmp_path: Path):
    import subprocess

    config_path, instance_id, connection_id, _job_id = _seed(tmp_path)
    conn = db.connect(tmp_path / "data" / "jobforge.sqlite3")
    db.disable_agent_connection(conn, connection_id)
    conn.close()
    repo = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "jobforge",
            "--config",
            str(config_path),
            "mcp",
            "serve",
            "--connection",
            connection_id,
            "--instance",
            instance_id,
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 3
    assert "CONNECTION_DISABLED" in result.stderr
    assert result.stdout == ""


def test_mcp_legacy_initialization_can_call_status(tmp_path: Path):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    config_path, instance_id, connection_id, _job_id = _seed(tmp_path)
    repo = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "jobforge",
            "--config",
            str(config_path),
            "mcp",
            "serve",
            "--connection",
            connection_id,
            "--instance",
            instance_id,
        ],
        cwd=str(repo),
    )

    async def exercise() -> None:
        async with Client(params, mode="legacy") as client:
            status = await client.call_tool("jobforge_status", {})
            assert status.is_error is False
            assert status.structured_content["data"]["interface_version"] == "1"

    asyncio.run(exercise())


def test_mcp_memory_and_resume_request_require_explicit_scopes(tmp_path: Path):
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters

    config_path, instance_id, connection_id, job_id = _seed(tmp_path)
    conn = db.connect(tmp_path / "data" / "jobforge.sqlite3")
    db.update_agent_connection_scopes(
        conn,
        connection_id,
        ["memory:read_write", "proposals:create", "profile:resume_context"],
    )
    second_job_id = db.upsert_job(
        conn,
        Job(
            source="greenhouse",
            source_id="mcp-job-2",
            company="MCP Two",
            title="Platform Engineer",
            description="Rust platform role",
        ),
    )
    conn.close()
    repo = Path(__file__).resolve().parents[1]
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "jobforge",
            "--config",
            str(config_path),
            "mcp",
            "serve",
            "--connection",
            connection_id,
            "--instance",
            instance_id,
        ],
        cwd=str(repo),
    )

    async def exercise() -> None:
        async with Client(params) as client:
            remembered = await client.call_tool(
                "jobforge_remember", {"key": "preference", "value": "remote roles"}
            )
            assert remembered.is_error is False
            assert remembered.structured_content["data"]["key"] == "preference"

            blocked = await client.call_tool(
                "jobforge_remember",
                {"key": "contact", "value": "private@example.com"},
            )
            assert blocked.is_error is True
            assert blocked.structured_content["error"]["code"] == "INVALID_ARGUMENT"

            recalled = await client.call_tool(
                "jobforge_recall", {"query": "remote"}
            )
            assert recalled.is_error is False
            assert recalled.structured_content["data"]["memories"][0]["value"] == "remote roles"

            request = await client.call_tool(
                "jobforge_request_resume",
                {"job_id": job_id, "idempotency_key": "resume-test-1"},
            )
            assert request.is_error is False
            data = request.structured_content["data"]
            assert data["state"] == "pending_approval"
            assert data["submission_performed_by_jobforge"] is False
            operation_id = data["operation_id"]

            second = await client.call_tool(
                "jobforge_request_resume",
                {"job_id": job_id, "idempotency_key": "resume-test-1"},
            )
            assert second.structured_content["data"]["operation_id"] == operation_id

            conflict = await client.call_tool(
                "jobforge_request_resume",
                {"job_id": second_job_id, "idempotency_key": "resume-test-1"},
            )
            assert conflict.is_error is True
            assert conflict.structured_content["error"]["code"] == "IDEMPOTENCY_CONFLICT"

            fetched = await client.call_tool(
                "jobforge_get_operation", {"operation_id": operation_id}
            )
            assert fetched.is_error is False
            assert fetched.structured_content["data"]["state"] == "pending_approval"

            listed = await client.call_tool("jobforge_list_operations", {})
            assert listed.is_error is False
            assert listed.structured_content["data"]["operations"][0]["operation_id"] == operation_id

            forgotten = await client.call_tool(
                "jobforge_forget", {"key": "preference"}
            )
            assert forgotten.is_error is False
            assert forgotten.structured_content["data"]["deleted"] is True

    asyncio.run(exercise())
