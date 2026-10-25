from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from jobforge.matching.jd import parse_jd
from jobforge.models import Job, Profile
from jobforge.tailoring.ats_check import ats_check
from jobforge.tailoring.tailor import build_data, select_facts


PROFILE = Profile(
    id=1,
    first_name="Jane",
    last_name="Doe",
    email="jane@example.com",
    phone="+1 555 123 4567",
    location="New York, NY",
    links={"github": "https://github.com/jane"},
    summary="Software engineer focused on backend systems.",
    skills=["Python", "FastAPI", "PostgreSQL", "Docker"],
    experience=[
        {
            "title": "Software Engineer",
            "company": "Acme",
            "location": "New York, NY",
            "start": "2024",
            "end": "Present",
            "bullets": ["Built a Python API that cut latency 40%", "Shipped a Docker deployment"],
        }
    ],
    education=[{"institution": "Example University", "degree": "Bachelor of Science", "major": "Computer Science", "start": "2022", "end": "2026"}],
    facts=[
        {"id": "fact-001", "section": "experience", "title": "Software Engineer", "company": "Acme", "text": "Built a Python API that cut latency 40%", "skills": ["Python", "FastAPI"]},
        {"id": "fact-002", "section": "experience", "title": "Software Engineer", "company": "Acme", "text": "Shipped a Docker deployment", "skills": ["Docker"]},
    ],
)


def test_fact_selection_prefers_relevant_bullet():
    jd = parse_jd("Requirements:\n- Python\n- FastAPI\n- Kubernetes", title_hint="Backend Engineer")
    selected, ids = select_facts(PROFILE, jd)
    assert "fact-001" in ids


def test_build_data_has_no_invented_fields():
    jd = parse_jd("Requirements:\n- Python", title_hint="Backend Engineer")
    selected, _ = select_facts(PROFILE, jd)
    data = build_data(PROFILE, jd, selected)
    assert data["name"] == "Jane Doe"
    assert data["skills"][0] in {"Python", "FastAPI"}
    assert data["experience"][0]["company"] == "Acme"


@pytest.mark.skipif(shutil.which("typst") is None, reason="typst not installed")
def test_render_and_ats_check(tmp_path: Path):
    from jobforge.tailoring.tailor import render

    jd = parse_jd("Requirements:\n- Python", title_hint="Backend Engineer")
    selected, _ = select_facts(PROFILE, jd)
    data = build_data(PROFILE, jd, selected)
    pdf, typ, data_path = render(data, tmp_path)
    assert pdf.exists() and typ.exists() and data_path.exists()
    result = ats_check(pdf)
    assert result["pages"] == 1
    assert result["chars"] > 200
    assert result["images"] == 0
