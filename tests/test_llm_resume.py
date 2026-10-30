from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from jobforge.matching.jd import parse_jd
from jobforge.models import Job, Profile
from jobforge.tailoring.llm_resume import generate_llm_resume
from jobforge.tailoring.tailor import build_data, select_facts, tailor_resume


def _profile() -> Profile:
    return Profile(
        id=1,
        first_name="Jane",
        last_name="Doe",
        email="jane@example.com",
        phone="+1 555 123 4567",
        location="New York, NY",
        summary="Software engineer focused on backend systems.",
        skills=["Python", "FastAPI", "PostgreSQL", "Docker"],
        experience=[
            {
                "title": "Software Engineer",
                "company": "Acme",
                "location": "New York, NY",
                "start": "2024",
                "end": "Present",
                "bullets": [
                    "Built a Python API that cut latency 40%",
                    "Shipped a Docker deployment",
                ],
            }
        ],
        projects=[
            {
                "title": "Alpha",
                "tech_line": "Python, FastAPI",
                "bullets": ["Built a FastAPI service."],
                "url": "https://github.com/jane/alpha",
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
                "text": "Built a Python API that cut latency 40%",
                "skills": ["Python", "FastAPI"],
            },
            {
                "id": "fact-002",
                "section": "experience",
                "title": "Software Engineer",
                "company": "Acme",
                "text": "Shipped a Docker deployment",
                "skills": ["Docker"],
            },
            {
                "id": "fact-003",
                "section": "projects",
                "title": "Alpha",
                "text": "Built a FastAPI service.",
                "skills": ["FastAPI"],
            },
        ],
    )


def _job() -> Job:
    return Job(
        source="lever",
        source_id="llm-1",
        company="Acme",
        title="Backend Engineer",
        location="Remote",
        description="Requirements:\n- Python\n- FastAPI\n- Docker",
    )


class FakeLLM:
    available = True
    model = "fake-model"

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def json_chat(self, system, user, **kwargs):
        self.calls.append({"system": system, "user": user, **kwargs})
        return self.payload

    def chat(self, *args, **kwargs):
        return None


def _valid_payload():
    return {
        "summary": {
            "text": "Backend engineer shipping Python APIs.",
            "source_ids": ["fact-001"],
        },
        "experience": [
            {
                "source_key": "exp:0",
                "bullets": [
                    {
                        "text": "Built a Python API that cut latency 40%.",
                        "source_ids": ["fact-001"],
                    }
                ],
            }
        ],
        "projects": [],
        "skill_groups": [{"label": "Languages", "items": ["Python", "FastAPI"]}],
    }


def test_generate_llm_resume_accepts_cited_verified_output():
    profile = _profile()
    job = _job()
    jd = parse_jd(job.description, title_hint=job.title)
    selected, _ = select_facts(profile, jd)
    baseline = build_data(profile, jd, selected)
    fake = FakeLLM(_valid_payload())

    outcome = generate_llm_resume(profile, job, jd, fake, baseline)

    assert outcome.used is True
    assert outcome.data is not None
    assert outcome.data["summary"] == "Backend engineer shipping Python APIs."
    assert outcome.data["experience"][0]["company"] == "Acme"
    assert outcome.data["experience"][0]["bullets"] == [
        "Built a Python API that cut latency 40%."
    ]
    assert "fact-001" in outcome.source_ids
    assert fake.calls and "SOURCE:" in fake.calls[0]["user"]


def test_generate_llm_resume_drops_invented_claims():
    profile = _profile()
    job = _job()
    jd = parse_jd(job.description, title_hint=job.title)
    selected, _ = select_facts(profile, jd)
    baseline = build_data(profile, jd, selected)
    fake = FakeLLM(
        {
            "summary": {"text": "10 years of Go leadership.", "source_ids": ["fact-001"]},
            "experience": [
                {
                    "source_key": "exp:0",
                    "bullets": [
                        {
                            "text": "Led Google acquisition saving $50M.",
                            "source_ids": ["fact-001"],
                        }
                    ],
                }
            ],
            "projects": [],
            "skill_groups": [{"label": "Languages", "items": ["Go", "Python"]}],
        }
    )

    outcome = generate_llm_resume(profile, job, jd, fake, baseline)

    # The only remaining LLM section is the valid Python skill; the invented
    # summary and bullet are dropped.
    assert outcome.used is True
    assert outcome.data is not None
    assert outcome.data["summary"] == profile.summary
    assert outcome.data["experience"][0]["bullets"] == [
        "Built a Python API that cut latency 40%",
        "Shipped a Docker deployment",
    ]
    assert any("unsupported" in warning.lower() for warning in outcome.warnings)
    rendered = json.dumps(outcome.data)
    assert "Google" not in rendered
    assert "$50M" not in rendered
    assert "10 years" not in rendered


@pytest.mark.skipif(shutil.which("typst") is None, reason="typst not installed")
def test_tailor_resume_llm_mode_renders_and_records_mode(tmp_path: Path):
    profile = _profile()
    fake = FakeLLM(_valid_payload())
    result = tailor_resume(
        profile, _job(), tmp_path, llm=fake, mode="llm", write_cover_letter=False
    )
    assert result.llm_used is True
    assert result.mode == "llm"
    assert result.llm_model == "fake-model"
    assert result.summary == "Backend engineer shipping Python APIs."
    assert Path(result.pdf_path).is_file()
    data = json.loads(Path(result.data_path).read_text(encoding="utf-8"))
    assert data["summary"] == "Backend engineer shipping Python APIs."
    assert data["experience"][0]["bullets"] == [
        "Built a Python API that cut latency 40%."
    ]


def test_tailor_resume_llm_mode_requires_provider(tmp_path: Path):
    with pytest.raises(ValueError, match="no model provider"):
        tailor_resume(
            _profile(), _job(), tmp_path, llm=None, mode="llm", write_cover_letter=False
        )


def test_tailor_resume_auto_falls_back_when_llm_is_invalid(tmp_path: Path):
    profile = _profile()
    fake = FakeLLM({"summary": "Invented 12 years of Rust.", "experience": []})
    result = tailor_resume(
        profile, _job(), tmp_path, llm=fake, mode="auto", write_cover_letter=False
    )
    assert result.llm_used is False
    assert result.summary == profile.summary
    assert any(
        "supported" in warning.lower() or "no usable" in warning.lower()
        for warning in result.warnings
    )
