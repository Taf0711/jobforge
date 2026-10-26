from __future__ import annotations

from jobforge.matching.jd import parse_jd
from jobforge.models import Job, Profile
from jobforge.tailoring.tailor import build_data, select_projects


def _profile() -> Profile:
    return Profile(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        phone="+1 555 0100",
        location="New York City, NY",
        links={"linkedin": "https://linkedin.com/in/ada", "github": "https://github.com/ada"},
        work_auth={"us_citizen": True},
        education=[
            {
                "institution": "Example University",
                "degree": "Bachelor of Arts",
                "major": "Computer Science",
                "start": "2022-08",
                "end": "2026-06",
                "courses": [f"Course {i}" for i in range(10)],
            }
        ],
        experience=[
            {"title": f"Role {i}", "company": f"Company {i}", "start": "2026-01", "end": "2026-02", "bullets": [f"Bullet {i}"]}
            for i in range(4)
        ],
        projects=[
            {
                "title": "Dead Project",
                "tech_line": "Python",
                "dead": True,
                "bullets": ["Should not render"],
            },
            {
                "title": "Splice",
                "tech_line": "Go, Typed Pipeline",
                "url": "https://github.com/taf0711/splice",
                "tags": ["AI", "Go"],
                "best_for": ["backend"],
                "bullets": ["Shipped Splice."],
            },
            {
                "title": "N-Body",
                "tech_line": "C++, SIMD",
                "tags": ["systems", "HPC"],
                "best_for": ["systems"],
                "bullets": ["Optimized N-Body."],
            },
            {
                "title": "JIT",
                "tech_line": "Python, FastAPI",
                "tags": ["security", "backend"],
                "best_for": ["security"],
                "bullets": ["Built JIT."],
            },
            {
                "title": "Extra",
                "tech_line": "Rust",
                "tags": ["systems"],
                "best_for": ["systems"],
                "bullets": ["Built Extra."],
            },
        ],
        skills=["Python", "C", "C++", "LLM", "Git", "scikit-learn", "WeirdSkill"],
    )


def test_resume_forge_layout_data_contract():
    profile = _profile()
    jd = parse_jd("Requirements:\n- Python\n- LLM", title_hint="Backend Engineer")

    data = build_data(_profile(), jd, [])

    # Header is phone, email, links, then explicit citizenship; no location.
    assert [item["text"] for item in data["header"]][-1] == "U.S. Citizen"
    assert all(item["text"] != "New York City, NY" for item in data["header"])
    assert data["education"][0]["degree_line"] == "Bachelor of Arts in Computer Science"
    assert data["education"][0]["start"] == "August 2022"
    assert data["education"][0]["end"] == "June 2026"
    assert len(data["education"][0]["courses"]) == 8

    # The base resume keeps three experience entries.
    assert [entry["company"] for entry in data["experience"]] == ["Company 0", "Company 1", "Company 2"]

    # Dead projects are excluded; at most three live projects; tech line/URL survive.
    names = [project["name"] for project in data["projects"]]
    assert "Dead Project" not in names
    assert len(names) == 3
    assert any(project["url"].startswith("https://github.com/") for project in data["projects"])

    # Resume-forge skill groups, C/C++ collapsing, JD priority, and no duplicate groups.
    labels = [group["label"] for group in data["skill_groups"]]
    assert labels[:4] == ["Languages", "Technologies", "Concepts", "Developer Tools"]
    languages = next(group["items"] for group in data["skill_groups"] if group["label"] == "Languages")
    assert "C/C++" in languages
    assert "C" not in languages and "C++" not in languages
    concepts = next(group["items"] for group in data["skill_groups"] if group["label"] == "Concepts")
    assert "LLM" in concepts
    assert data["skills"] == [item for group in data["skill_groups"] for item in group["items"]]


def test_dead_projects_are_not_selected():
    profile = _profile()
    jd = parse_jd("Requirements:\n- Python", title_hint="Backend Engineer")

    selected = select_projects(profile, jd)

    assert len(selected) == 3
    assert all(project["title"] != "Dead Project" for project in selected)
