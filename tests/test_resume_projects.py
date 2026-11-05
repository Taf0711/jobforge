from __future__ import annotations

from jobforge.matching.resume import parse_resume


def test_projects_with_pipe_headers_stay_separate_and_keep_tech_lines():
    text = """
Tafseer Haque
tafseer@example.com

Projects
N-Body Galaxy Simulator | C++, NEON SIMD, Multithreading, WebGPU, Barnes-Hut
• Optimized an N-body simulator from a naive loop to a multithreaded tree
• Shipped a WebGPU compute demo rendering galaxy collisions
Splice | Go, npm CLI, Typed Execution Pipeline | GitHub
• Shipped Splice, a Go terminal coding agent
• Gated completion on deterministic verification
JIT Access & Policy Gateway | Python, FastAPI, PostgreSQL, Redis, OPA
• Built a Just-In-Time access system with time-boxed grants
• Enforced policy-as-code via OPA/Rego
"""
    profile = parse_resume(text, "tafseer")

    assert [project["title"] for project in profile.projects] == [
        "N-Body Galaxy Simulator",
        "Splice",
        "JIT Access & Policy Gateway",
    ]
    assert profile.projects[0]["tech_line"] == (
        "C++, NEON SIMD, Multithreading, WebGPU, Barnes-Hut"
    )
    assert profile.projects[1]["tech_line"] == (
        "Go, npm CLI, Typed Execution Pipeline"
    )
    assert profile.projects[1]["url"] == ""
    assert [len(project["bullets"]) for project in profile.projects] == [2, 2, 2]
    assert all(
        "|" not in bullet
        for project in profile.projects
        for bullet in project["bullets"]
    )
    assert profile.projects[0]["bullets"][-1].endswith("galaxy collisions")
    assert profile.projects[1]["bullets"][0].startswith("Shipped Splice")


def test_projects_with_title_tech_and_date_layout_are_cohesive():
    text = """
Ada Lovelace
ada@example.com

Projects
Semantic Cache Proxy
Rust, axum/tokio, HNSW, ONNX Runtime, SQLite
2024 - 2025
• Built an OpenAI-compatible cache proxy
• Implemented adaptive similarity thresholds
https://github.com/ada/cache
Edge Scheduler | Python, NumPy
• Built a reinforcement-learning scheduler
"""
    profile = parse_resume(text, "ada")

    assert len(profile.projects) == 2
    cache = profile.projects[0]
    assert cache["title"] == "Semantic Cache Proxy"
    assert cache["tech_line"] == "Rust, axum/tokio, HNSW, ONNX Runtime, SQLite"
    assert (cache["start"], cache["end"]) == ("2024", "2025")
    assert cache["url"] == "https://github.com/ada/cache"
    assert len(cache["bullets"]) == 2
    assert profile.projects[1]["title"] == "Edge Scheduler"
    assert profile.projects[1]["tech_line"] == "Python, NumPy"
    assert profile.projects[1]["bullets"] == [
        "Built a reinforcement-learning scheduler"
    ]


def test_project_facts_use_project_titles_and_do_not_merge_headers():
    text = """
Ada Lovelace
ada@example.com

Projects
Alpha | Python, FastAPI
• Built alpha
Beta | Go, Docker
• Built beta
"""
    profile = parse_resume(text, "ada")

    project_facts = [
        fact for fact in profile.facts if fact.get("section") == "projects"
    ]
    assert [fact["title"] for fact in project_facts] == ["Alpha", "Beta"]
    assert [fact["text"] for fact in project_facts] == ["Built alpha", "Built beta"]


def test_parsed_projects_render_with_titles_and_tech_lines():
    from jobforge.matching.jd import JD
    from jobforge.tailoring.tailor import build_data

    text = """
Ada Lovelace
ada@example.com

Projects
Alpha | Python, FastAPI
• Built alpha
Beta | Go, Docker
• Built beta
"""
    profile = parse_resume(text, "ada")
    data = build_data(profile, JD(title="Backend Engineer"), [])

    assert [project["name"] for project in data["projects"]] == ["Alpha", "Beta"]
    assert [project["tech_line"] for project in data["projects"]] == [
        "Python, FastAPI",
        "Go, Docker",
    ]


def test_title_only_project_headers_start_new_entries_without_punctuation():
    text = """
Ada Lovelace
ada@example.com

Projects
Alpha
• Built alpha without a trailing period
Beta
• Built beta
"""
    profile = parse_resume(text, "ada")

    assert [project["title"] for project in profile.projects] == ["Alpha", "Beta"]
    assert [project["bullets"] for project in profile.projects] == [
        ["Built alpha without a trailing period"],
        ["Built beta"],
    ]
