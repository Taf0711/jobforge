from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jobforge import db
from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.web.answers_routes import answers_path, create_answers_router


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


def _client(tmp_path: Path) -> tuple[TestClient, Config]:
    cfg = _config(tmp_path)
    app = FastAPI()
    app.include_router(create_answers_router(cfg))
    return TestClient(app), cfg


def test_page_renders_builtin_categories(tmp_path: Path):
    client, _ = _client(tmp_path)
    response = client.get("/answers")
    assert response.status_code == 200
    body = response.text
    assert "Answer bank" in body
    for label in [
        "Name",
        "Email",
        "Phone",
        "Location",
        "Current company",
        "Current title",
        "Work authorization",
        "Sponsorship",
        "Salary",
        "Notice period",
        "Relocation",
        "Clearance",
        "Voluntary disclosures",
    ]:
        assert label in body
    assert "substring" in body
    assert "never guessed" in body


def test_post_adds_override_and_writes_private_atomic_file(tmp_path: Path):
    client, cfg = _client(tmp_path)
    response = client.post(
        "/answers",
        data={"key": "notice period", "value": "2 weeks"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/answers"

    path = answers_path(cfg)
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8")) == {"notice period": "2 weeks"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(".answers-*.tmp")) == []

    assert client.get("/api/answers").json() == {"notice period": "2 weeks"}

    # A second POST updates in place rather than duplicating.
    client.post("/answers", data={"key": "notice period", "value": "3 weeks"})
    assert client.get("/api/answers").json() == {"notice period": "3 weeks"}


def test_get_api_returns_overrides(tmp_path: Path):
    client, cfg = _client(tmp_path)
    answers_path(cfg).write_text(
        json.dumps({"salary": "150000", "notice period": "2 weeks"}), encoding="utf-8"
    )
    response = client.get("/api/answers")
    assert response.status_code == 200
    assert response.json() == {"salary": "150000", "notice period": "2 weeks"}


def test_put_replaces_all_overrides(tmp_path: Path):
    client, cfg = _client(tmp_path)
    client.put("/api/answers", json={"old": "value"})

    response = client.put(
        "/api/answers",
        json={"salary": "150000", "relocation": "Yes"},
    )
    assert response.status_code == 200
    assert response.json() == {"salary": "150000", "relocation": "Yes"}
    assert json.loads(answers_path(cfg).read_text(encoding="utf-8")) == {
        "relocation": "Yes",
        "salary": "150000",
    }
    assert client.get("/api/answers").json() == {"relocation": "Yes", "salary": "150000"}


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "not-an-object",
        42,
        {"": "value"},
        {"key": ""},
        {"k" * 121: "value"},
        {"key": "v" * 501},
        {f"key-{i}": "value" for i in range(201)},
        {"<script>": "value"},
        {"key": "a > b"},
        {"key": "line\nbreak"},
        {"key": "tab\tseparated"},
        {"key": 123},
        {"key": None},
    ],
)
def test_put_rejects_invalid_payloads(tmp_path: Path, payload):
    client, cfg = _client(tmp_path)
    response = client.put("/api/answers", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"]
    assert not answers_path(cfg).exists()


def test_put_rejects_malformed_json(tmp_path: Path):
    client, cfg = _client(tmp_path)
    response = client.put(
        "/api/answers",
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert not answers_path(cfg).exists()


def test_post_rejects_xss_value(tmp_path: Path):
    client, cfg = _client(tmp_path)
    response = client.post(
        "/answers",
        data={"key": "why us", "value": "<script>alert(1)</script>"},
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert not answers_path(cfg).exists()


def test_page_escapes_hand_edited_values(tmp_path: Path):
    client, cfg = _client(tmp_path)
    answers_path(cfg).write_text(
        json.dumps({"why us": '<img src=x onerror=alert(1)>'}), encoding="utf-8"
    )
    body = client.get("/answers").text
    assert "<img src=x" not in body
    assert "&lt;img src=x" in body


def test_traversal_like_key_stays_inside_data_dir(tmp_path: Path):
    client, cfg = _client(tmp_path)
    key = "../../../etc/passwd"
    response = client.post(
        "/answers", data={"key": key, "value": "nope"}, follow_redirects=False
    )
    assert response.status_code == 303

    assert answers_path(cfg).exists()
    assert client.get("/api/answers").json() == {key: "nope"}
    assert not (tmp_path / "etc" / "passwd").exists()
    assert not (tmp_path / "data" / ".." / "etc").exists()


def test_malformed_answers_file_reads_as_empty(tmp_path: Path):
    client, cfg = _client(tmp_path)
    answers_path(cfg).write_text("{broken", encoding="utf-8")
    assert client.get("/api/answers").json() == {}
