from __future__ import annotations

from pathlib import Path

from jobforge.config import BrowserConfig, Config, EmailConfig, LLMConfig, ServerConfig
from jobforge.models import Job
from jobforge.sources import registry as source_registry


class _FakeSource:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def fetch(self, board: str, **kwargs):
        self.calls.append(board)
        return [
            Job(
                source="lever",
                source_id=board,
                company="Palantir",
                title="Software Engineer",
                description="Python, distributed systems",
            )
        ]


def _config(tmp_path: Path, sources: dict | None = None) -> Config:
    cfg = Config(
        data_dir=tmp_path / "data",
        output_dir=tmp_path / "output",
        db_path=tmp_path / "data" / "test.sqlite3",
        profile_path=None,
        resume_path=None,
        sources=sources or {},
        preferences={},
        llm=LLMConfig(),
        email=EmailConfig(),
        browser=BrowserConfig(),
        server=ServerConfig(),
    )
    cfg.ensure_dirs()
    return cfg


def test_board_selector_is_narrow_and_known_ats_only():
    assert source_registry.parse_board_selector("lever:palantir") == ("lever", "palantir")
    assert source_registry.parse_board_selector("greenhouse:affirm") == ("greenhouse", "affirm")
    assert source_registry.parse_board_selector("https://jobs.lever.co/palantir") is None
    assert source_registry.parse_board_selector("lever:palantir:extra") is None
    assert source_registry.parse_board_selector("lever:pal*antir") is None


def test_explicit_board_selector_fetches_unconfigured_board(tmp_path, monkeypatch):
    fake = _FakeSource()
    monkeypatch.setitem(source_registry.SOURCES, "lever", fake)
    cfg = _config(tmp_path)

    report = source_registry.fetch_configured(cfg, only=["lever:palantir"])

    assert fake.calls == ["palantir"]
    assert report.errors == []
    assert report.per_source["lever:palantir"] == 1
    assert report.jobs[0].company == "Palantir"


def test_source_name_still_uses_configured_boards(tmp_path, monkeypatch):
    fake = _FakeSource()
    monkeypatch.setitem(source_registry.SOURCES, "lever", fake)
    cfg = _config(tmp_path, {"lever": {"boards": ["metlife"]}})

    report = source_registry.fetch_configured(cfg, only=["lever"])

    assert fake.calls == ["metlife"]
    assert report.errors == []


def test_invalid_selector_does_not_fetch(tmp_path, monkeypatch):
    fake = _FakeSource()
    monkeypatch.setitem(source_registry.SOURCES, "lever", fake)
    cfg = _config(tmp_path)

    report = source_registry.fetch_configured(cfg, only=["lever:palantir:extra"])

    assert fake.calls == []
    assert report.errors and "Unknown source" in report.errors[0]


def test_registry_board_selector_uses_entry_kwargs(tmp_path, monkeypatch):
    from jobforge.scan import registry as scan_registry

    fake = _FakeSource()
    registry = tmp_path / "scan_sources.yml"
    registry.write_text(
        "sources:\n  - name: palantir\n    type: lever\n    site: palantir\n",
        encoding="utf-8",
    )
    cfg = _config(tmp_path)
    cfg.scan = {"sources_path": str(registry)}
    monkeypatch.setattr(
        scan_registry,
        "_source_for",
        lambda entry: (fake, str(entry["site"]), {}, "direct_ats"),
    )

    report = scan_registry.fetch_registry(cfg, only=["lever:palantir"])

    assert fake.calls == ["palantir"]
    assert report.errors == []
    assert report.per_source["palantir"] == 1


def test_registry_board_selector_falls_back_to_known_ats(tmp_path, monkeypatch):
    from jobforge.scan import registry as scan_registry

    fake = _FakeSource()
    monkeypatch.setitem(source_registry.SOURCES, "lever", fake)
    registry = tmp_path / "scan_sources.yml"
    registry.write_text("sources: []\n", encoding="utf-8")
    cfg = _config(tmp_path)
    cfg.scan = {"sources_path": str(registry)}

    report = scan_registry.fetch_registry(cfg, only=["lever:palantir"])

    assert fake.calls == ["palantir"]
    assert report.errors == []
    assert report.per_source["lever:palantir"] == 1


def test_lever_source_merges_description_lists_and_additional(monkeypatch):
    from jobforge.sources import lever as lever_module

    monkeypatch.setattr(
        lever_module,
        "http_get_json",
        lambda url: [
            {
                "id": "1",
                "text": "Backend Engineer",
                "categories": {"location": "New York, NY"},
                "descriptionPlain": "About the team.",
                "lists": [
                    {
                        "text": "Requirements",
                        "content": "<ul><li>Python</li><li>Distributed systems</li></ul>",
                    }
                ],
                "additionalPlain": "Equal opportunity employer.",
            }
        ],
    )

    jobs = lever_module.LeverSource().fetch("palantir")

    assert len(jobs) == 1
    description = jobs[0].description
    assert "About the team." in description
    assert "Requirements" in description
    assert "Python" in description
    assert "Distributed systems" in description
    assert "Equal opportunity employer." in description
