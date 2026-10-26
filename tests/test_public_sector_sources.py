from __future__ import annotations

import jobforge.sources.nyc as nyc_source
import jobforge.sources.usajobs as usajobs_source
from jobforge.sources.registry import SINGLETON_SOURCES, SOURCES
from jobforge.sources.nyc import NYCOpenDataSource
from jobforge.sources.usajobs import USAJobsSource


def test_public_sector_sources_are_registered():
    assert "nyc_opendata" in SOURCES
    assert "usajobs" in SOURCES
    assert SINGLETON_SOURCES == {"nyc_opendata", "usajobs"}


def test_usajobs_requires_key_and_parses(monkeypatch):
    source = USAJobsSource()
    monkeypatch.delenv("USAJOBS_API_KEY", raising=False)
    monkeypatch.delenv("USAJOBS_EMAIL", raising=False)
    assert source.fetch() == []

    monkeypatch.setenv("USAJOBS_API_KEY", "key")
    monkeypatch.setenv("USAJOBS_EMAIL", "user@example.com")
    payload = {
        "SearchResult": {
            "SearchResultItems": [
                {
                    "MatchedObjectDescriptor": {
                        "PositionID": "123",
                        "PositionTitle": "Software Engineer",
                        "DepartmentName": "US Courts",
                        "PositionLocation": [{"LocationName": "New York, NY"}],
                        "PositionURI": "https://www.usajobs.gov/job/123",
                        "ApplyURI": [
                            "https://apply.usastaffing.gov/ViewQuestionnaire/123"
                        ],
                        "PositionRemuneration": [
                            {"MinimumRange": "90000", "MaximumRange": "120000"}
                        ],
                        "PublicationStartDate": "2026-10-01",
                        "UserArea": {
                            "Details": {
                                "JobSummary": "Build <b>software</b>.",
                                "QualificationSummary": "Python.",
                            }
                        },
                    }
                }
            ]
        }
    }
    monkeypatch.setattr(
        usajobs_source, "http_get_json", lambda *args, **kwargs: payload
    )

    jobs = source.fetch(keyword="software engineer", limit=5)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "usajobs"
    assert job.source_id == "123"
    assert job.company == "US Courts"
    assert job.title == "Software Engineer"
    assert job.location == "New York, NY"
    assert job.salary_min == 90000
    assert job.salary_max == 120000
    assert job.url == "https://www.usajobs.gov/job/123"
    assert job.apply_url == "https://apply.usastaffing.gov/ViewQuestionnaire/123"
    assert "Build software" in job.description
    assert "<b>" not in job.description
    assert job.posted_at == "2026-10-01T00:00:00"


def test_nyc_opendata_parses_canonical_job_link(monkeypatch):
    payload = [
        {
            "job_id": "802240",
            "agency": "DEPARTMENT OF TRANSPORTATION",
            "business_title": "SOFTWARE ENGINEER",
            "work_location": "55 Water St Ny Ny",
            "salary_range_from": "72595",
            "salary_range_to": "92976",
            "job_description": "Build public-sector systems.",
            "minimum_qual_requirements": "CS degree.",
            "preferred_skills": "Python.",
            "posting_date": "2026-09-23T00:00:00.000",
        }
    ]
    captured: dict[str, str] = {}

    def fake_get(url, *args, **kwargs):
        captured["url"] = url
        return payload

    monkeypatch.setattr(nyc_source, "http_get_json", fake_get)

    jobs = NYCOpenDataSource().fetch(query="software", limit=5)

    assert "q=software" in captured["url"]

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "nyc_opendata"
    assert job.source_id == "802240"
    assert job.company == "DEPARTMENT OF TRANSPORTATION"
    assert job.title == "SOFTWARE ENGINEER"
    assert job.url == "https://cityjobs.nyc.gov/job/802240"
    assert job.apply_url == job.url
    assert job.salary_min == 72595
    assert job.salary_max == 92976
    assert "Build public-sector systems." in job.description
    assert job.posted_at == "2026-09-23T00:00:00"
