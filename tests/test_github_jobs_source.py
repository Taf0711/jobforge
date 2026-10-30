from __future__ import annotations

from types import SimpleNamespace

from jobforge.sources import github_jobs as gh
from jobforge.sources.github_jobs import GitHubJobsSource
from jobforge.sources.registry import SOURCES, configured_boards
from jobforge.web.setup_presets import build_sources, source_board_count


def _fake_get(text: str):
    def fake(url: str, **kwargs):
        return SimpleNamespace(content=text.encode("utf-8"))
    return fake


def test_github_jobs_parses_direct_employer_links_and_grouped_companies(monkeypatch):
    text = """
    <table>
      <thead><tr><th>Company</th><th>Role</th><th>Location</th><th>Application</th><th>Age</th></tr></thead>
      <tbody>
        <tr>
          <td><strong><a href="https://simplify.jobs/c/Acme">🔥 Acme</a></strong></td>
          <td>Software Engineer</td><td>Remote - USA</td>
          <td><a href="https://boards.greenhouse.io/acme/jobs/1?utm_source=Simplify&ref=Simplify">Apply</a>
              <a href="https://simplify.jobs/p/abc">Simplify</a></td>
          <td>2d</td>
        </tr>
        <tr>
          <td>↳</td><td>Backend Engineer</td><td>New York, NY</td>
          <td><a href="https://jobs.lever.co/acme/2?utm_source=Simplify">Apply</a></td>
          <td>1d</td>
        </tr>
        <tr>
          <td>Aggregator Only</td><td>Software Engineer</td><td>Remote</td>
          <td><a href="https://simplify.jobs/p/only">Simplify</a></td><td>0d</td>
        </tr>
        <tr>
          <td>Closed Co</td><td>Software Engineer</td><td>Remote</td>
          <td>Closed</td><td>4d</td>
        </tr>
      </tbody>
    </table>
    """
    monkeypatch.setattr(gh, "http_get", _fake_get(text))
    jobs = GitHubJobsSource().fetch("acme/jobs")

    assert len(jobs) == 2
    assert jobs[0].company == "Acme"
    assert jobs[0].apply_url == "https://boards.greenhouse.io/acme/jobs/1"
    assert jobs[0].source == "github_jobs"
    assert jobs[0].posted_at is not None
    assert jobs[1].company == "Acme"
    assert jobs[1].apply_url == "https://jobs.lever.co/acme/2"
    assert all("simplify.jobs" not in job.apply_url for job in jobs)


def test_github_jobs_parses_markdown_tables(monkeypatch):
    text = """
| Company | Role | Location | Application | Age |
| --- | --- | --- | --- | --- |
| Beta | Backend Engineer | Remote | [Apply](https://jobs.lever.co/beta/1?utm_source=Simplify) [Simplify](https://simplify.jobs/p/1) | 1d |
| Gamma | Software Engineer | NYC | [Simplify](https://simplify.jobs/p/2) | 0d |
"""
    monkeypatch.setattr(gh, "http_get", _fake_get(text))
    jobs = GitHubJobsSource().fetch("https://github.com/acme/lists")

    assert len(jobs) == 1
    assert jobs[0].company == "Beta"
    assert jobs[0].apply_url == "https://jobs.lever.co/beta/1"


def test_github_jobs_is_registered_and_setup_preset_writes_repos(tmp_path):
    assert "github_jobs" in SOURCES
    cfg = SimpleNamespace(
        sources={
            "github_jobs": {
                "repos": ["SimplifyJobs/New-Grad-Positions", "SimplifyJobs/Summer2026-Internships"]
            }
        }
    )
    assert configured_boards(cfg, "github_jobs") == [
        "SimplifyJobs/New-Grad-Positions",
        "SimplifyJobs/Summer2026-Internships",
    ]

    sources = build_sources(["github_lists"], {})
    assert sources["github_jobs"]["repos"] == [
        "SimplifyJobs/New-Grad-Positions",
        "SimplifyJobs/Summer2026-Internships",
    ]
    assert source_board_count(sources) == 2
