from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

import jobforge.sources.workday as workday_source
from jobforge.automation.answers import AnswerBank
from jobforge.automation.workday import ACCOUNT_WALL_NOTE, WorkdayAdapter, _account_wall
from jobforge.models import Job, Preferences, Profile
from jobforge.sources.base import USER_AGENT, SourceError
from jobforge.sources.workday import WorkdaySource, parse_board, parse_posted_on


def _job() -> Job:
    return Job(
        source="workday",
        source_id="/job/Remote/Engineer_JR100",
        company="Acme",
        title="Engineer",
        url="https://acme.wd1.myworkdayjobs.com/en-US/External/job/Remote/Engineer_JR100",
        apply_url="https://acme.wd1.myworkdayjobs.com/en-US/External/job/Remote/Engineer_JR100",
    )


def _profile() -> Profile:
    return Profile(
        first_name="Ada",
        last_name="Lovelace",
        email="ada@example.com",
        phone="+1 555 0100",
        location="London",
    )


@pytest.fixture(scope="module")
def chromium_page():
    sync_api = pytest.importorskip("playwright.sync_api")
    playwright = None
    try:
        playwright = sync_api.sync_playwright().start()
        browser = playwright.chromium.launch(headless=True)
    except Exception as exc:
        if playwright is not None:
            playwright.stop()
        pytest.skip(f"Chromium unavailable: {exc}")
    page = browser.new_page()
    yield page
    browser.close()
    playwright.stop()


@pytest.mark.parametrize(
    ("board", "expected"),
    [
        ("acme|wd1|External", ("acme", "wd1", "External")),
        ("acme|External", ("acme", "wd1", "External")),
        ("Acme|WD3|External_Careers", ("acme", "wd3", "External_Careers")),
        ("https://acme.wd1.myworkdayjobs.com/en-US/External", ("acme", "wd1", "External")),
        ("https://acme.wd1.myworkdayjobs.com/External", ("acme", "wd1", "External")),
    ],
)
def test_parse_board_accepts_supported_shapes(board, expected):
    assert parse_board(board) == expected


@pytest.mark.parametrize(
    "board",
    [
        "acme",
        "acme|wd1|External|extra",
        "acme||External",
        "acme|wdX|External",
        "https://example.com/en-US/External",
    ],
)
def test_parse_board_rejects_invalid_shapes(board):
    with pytest.raises(ValueError):
        parse_board(board)


def test_parse_posted_on_handles_dates_and_relative_labels():
    assert parse_posted_on("2026-10-20T12:00:00.000Z") == "2026-10-20T12:00:00+00:00"
    assert parse_posted_on("Posted on 10/20/2026") == "2026-10-20T00:00:00+00:00"
    assert parse_posted_on("Posted 3 Days Ago") is None
    assert parse_posted_on("") is None


class _FixtureResponse:
    def __init__(self, data, status_code=200, headers=None):
        self._data = data
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size=32768):
        yield json.dumps(self._data).encode("utf-8")

    def close(self):
        self.closed = True


def _posting(external_path, title="Engineer", location="Remote, US"):
    return {
        "title": title,
        "externalPath": external_path,
        "locationsText": location,
        "postedOn": "2026-10-20T12:00:00.000Z",
    }


def test_fetch_parses_fixture_without_network(monkeypatch):
    fixture = {
        "total": 2,
        "jobPostings": [
            {
                "title": "Software Engineer",
                "externalPath": "/job/Remote/Software-Engineer_JR100",
                "locationsText": "Remote, US",
                "postedOn": "2026-10-20T12:00:00.000Z",
                "bulletFields": ["JR100"],
            },
            {
                "title": "Data Engineer",
                "externalPath": "/job/New-York/Data-Engineer_JR200",
                "locationsText": "New York, NY",
                "postedOn": "Posted 3 Days Ago",
                "bulletFields": ["JR200"],
            },
        ],
    }
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return _FixtureResponse(fixture)

    monkeypatch.setattr(workday_source.requests, "post", fake_post)

    jobs = WorkdaySource().fetch("acme|wd1|External", include_details=False)

    assert len(jobs) == 2
    first = jobs[0]
    assert first.source == "workday"
    assert first.title == "Software Engineer"
    assert first.company == "Acme"
    assert first.location == "Remote, US"
    assert first.source_id == "/job/Remote/Software-Engineer_JR100"
    assert first.url == "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Remote/Software-Engineer_JR100"
    assert first.apply_url == first.url
    assert first.posted_at == "2026-10-20T12:00:00+00:00"
    assert first.raw["bulletFields"] == ["JR100"]
    assert jobs[1].posted_at is None

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/jobs"
    assert kwargs["json"] == {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
    assert kwargs["timeout"] == 30
    assert kwargs["headers"]["User-Agent"] == USER_AGENT


@pytest.mark.parametrize(
    "url",
    [
        "https://acme.wd1.myworkdayjobs.com/en-US/External/job/Remote/Engineer_JR100",
        "https://acme.wd1.myworkdaysite.com/recruiting/acme/External/job/Remote/Engineer_JR100",
    ],
)
def test_adapter_matches_both_workday_hosts(url):
    assert WorkdayAdapter().matches(url)


def test_fetch_paginates_until_total(monkeypatch):
    pages = {
        0: {
            "total": 4,
            "jobPostings": [_posting("/job/A"), _posting("/job/B")],
        },
        2: {
            "total": 4,
            "jobPostings": [_posting("/job/C"), _posting("/job/D")],
        },
    }
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"])
        return _FixtureResponse(pages[kwargs["json"]["offset"]])

    monkeypatch.setattr(workday_source.requests, "post", fake_post)

    jobs = WorkdaySource().fetch("acme|wd1|External", page_size=2, include_details=False)

    assert [job.source_id for job in jobs] == ["/job/A", "/job/B", "/job/C", "/job/D"]
    assert [payload["offset"] for payload in calls] == [0, 2]
    assert [payload["limit"] for payload in calls] == [2, 2]


def test_fetch_stops_at_max_jobs(monkeypatch):
    pages = {
        0: {"total": 10, "jobPostings": [_posting("/job/A"), _posting("/job/B")]},
        2: {"total": 10, "jobPostings": [_posting("/job/C"), _posting("/job/D")]},
    }
    calls = []

    def fake_post(url, **kwargs):
        calls.append(kwargs["json"])
        return _FixtureResponse(pages[kwargs["json"]["offset"]])

    monkeypatch.setattr(workday_source.requests, "post", fake_post)

    jobs = WorkdaySource().fetch(
        "acme|wd1|External", max_jobs=3, page_size=2, include_details=False
    )

    assert [job.source_id for job in jobs] == ["/job/A", "/job/B", "/job/C"]
    assert [payload["offset"] for payload in calls] == [0, 2]


def test_fetch_deduplicates_external_path(monkeypatch):
    pages = {
        0: {
            "total": 3,
            "jobPostings": [_posting("/job/A"), _posting("/job/B")],
        },
        2: {"total": 3, "jobPostings": [_posting("/job/A")]},
    }

    monkeypatch.setattr(
        workday_source.requests,
        "post",
        lambda url, **kwargs: _FixtureResponse(pages[kwargs["json"]["offset"]]),
    )

    jobs = WorkdaySource().fetch("acme|wd1|External", page_size=2, include_details=False)

    assert [job.source_id for job in jobs] == ["/job/A", "/job/B"]


def test_fetch_populates_description_from_detail(monkeypatch):
    monkeypatch.setattr(
        workday_source.requests,
        "post",
        lambda *args, **kwargs: _FixtureResponse(
            {"total": 1, "jobPostings": [_posting("/job/Remote/Engineer_JR100")]}
        ),
    )
    detail_calls = []

    def fake_get_json(url, **kwargs):
        detail_calls.append((url, kwargs))
        return {
            "jobPostingInfo": {
                "jobDescription": "<p>Build reliable systems.</p><p>Hybrid role.</p>"
            }
        }

    monkeypatch.setattr(workday_source, "http_get_json", fake_get_json)

    jobs = WorkdaySource().fetch("acme|wd1|External")

    assert len(jobs) == 1
    assert jobs[0].description == "Build reliable systems.\nHybrid role."
    assert detail_calls == [
        (
            "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/External/job/Remote/Engineer_JR100",
            {"headers": {"Accept": "application/json"}},
        )
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"jobPostingInfo": {"externalDescription": "<p>External body</p>"}},
        {"jobDescription": "<p>Top-level body</p>"},
        {"description": "<p>Plain body</p>"},
    ],
)
def test_detail_parses_supported_description_shapes(monkeypatch, payload):
    monkeypatch.setattr(
        workday_source.requests,
        "post",
        lambda *args, **kwargs: _FixtureResponse(
            {"total": 1, "jobPostings": [_posting("/job/A")]}
        ),
    )
    monkeypatch.setattr(workday_source, "http_get_json", lambda *args, **kwargs: payload)

    jobs = WorkdaySource().fetch("acme|wd1|External")

    assert jobs[0].description in {"External body", "Top-level body", "Plain body"}


def test_detail_failure_keeps_list_job(monkeypatch):
    monkeypatch.setattr(
        workday_source.requests,
        "post",
        lambda *args, **kwargs: _FixtureResponse(
            {"total": 1, "jobPostings": [_posting("/job/A", title="Survivor")]}
        ),
    )

    def fail(url, **kwargs):
        raise SourceError("detail unavailable")

    monkeypatch.setattr(workday_source, "http_get_json", fail)

    jobs = WorkdaySource().fetch("acme|wd1|External")

    assert len(jobs) == 1
    assert jobs[0].title == "Survivor"
    assert jobs[0].description == ""


def test_include_details_false_skips_detail_requests(monkeypatch):
    monkeypatch.setattr(
        workday_source.requests,
        "post",
        lambda *args, **kwargs: _FixtureResponse(
            {"total": 1, "jobPostings": [_posting("/job/A")]}
        ),
    )
    detail_calls = []
    monkeypatch.setattr(
        workday_source, "http_get_json", lambda *args, **kwargs: detail_calls.append(args)
    )

    jobs = WorkdaySource().fetch("acme|wd1|External", include_details=False)

    assert jobs[0].description == ""
    assert detail_calls == []


def test_pagination_stops_when_a_page_has_no_new_jobs(monkeypatch):
    offsets = []

    def fake_post(url, **kwargs):
        offsets.append(kwargs["json"]["offset"])
        return _FixtureResponse(
            {"jobPostings": [{"externalPath": "/job/A", "title": "A"}]}
        )

    monkeypatch.setattr(workday_source.requests, "post", fake_post)
    monkeypatch.setattr(workday_source._LIMITER, "wait", lambda url: None)

    jobs = WorkdaySource().fetch(
        "acme|wd1|External", page_size=1, max_jobs=10, include_details=False
    )

    assert len(jobs) == 1
    assert offsets == [0, 1]


def test_detail_failure_keeps_the_list_job(monkeypatch):
    monkeypatch.setattr(
        workday_source.requests,
        "post",
        lambda *args, **kwargs: _FixtureResponse(
            {"jobPostings": [{"externalPath": "/job/A", "title": "A"}]}
        ),
    )
    monkeypatch.setattr(workday_source._LIMITER, "wait", lambda url: None)
    monkeypatch.setattr(
        workday_source,
        "http_get_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("bad Retry-After")),
    )

    jobs = WorkdaySource().fetch("acme|wd1|External", include_details=True)

    assert len(jobs) == 1
    assert jobs[0].description == ""


def test_fetch_honors_retry_after(monkeypatch):
    responses = [
        _FixtureResponse({}, status_code=429, headers={"Retry-After": "0"}),
        _FixtureResponse({"jobPostings": []}),
    ]
    calls = []
    sleeps = []
    all_responses = list(responses)
    limiter_calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(workday_source.requests, "post", fake_post)
    monkeypatch.setattr(workday_source.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(workday_source._LIMITER, "wait", limiter_calls.append)

    assert WorkdaySource().fetch("acme|wd1|External") == []
    assert len(calls) == 2
    assert limiter_calls == calls
    assert sleeps == [0.0]
    assert all(response.closed for response in all_responses)


def test_fetch_honors_http_date_retry_after(monkeypatch):
    retry_at = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    responses = [
        _FixtureResponse({}, status_code=429, headers={"Retry-After": retry_at}),
        _FixtureResponse({"jobPostings": []}),
    ]
    all_responses = list(responses)
    sleeps = []

    monkeypatch.setattr(workday_source.requests, "post", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(workday_source.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(workday_source._LIMITER, "wait", lambda url: None)

    assert WorkdaySource().fetch("acme|wd1|External") == []
    assert sleeps == [10.0]
    assert all(response.closed for response in all_responses)


def test_fetch_closes_5xx_responses(monkeypatch):
    responses = [_FixtureResponse({}, status_code=502) for _ in range(3)]
    all_responses = list(responses)

    monkeypatch.setattr(workday_source.requests, "post", lambda *args, **kwargs: responses.pop(0))
    monkeypatch.setattr(workday_source.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(workday_source._LIMITER, "wait", lambda url: None)

    with pytest.raises(SourceError):
        WorkdaySource().fetch("acme|wd1|External")
    assert all(response.closed for response in all_responses)


def test_fetch_rejects_oversized_body(monkeypatch):
    class BigResponse(_FixtureResponse):
        def iter_content(self, chunk_size=32768):
            yield b"{" + b"x" * 100

    limiter_calls = []
    response = BigResponse({})

    monkeypatch.setattr(workday_source, "MAX_BYTES", 10)
    monkeypatch.setattr(workday_source.requests, "post", lambda *args, **kwargs: response)
    monkeypatch.setattr(workday_source._LIMITER, "wait", limiter_calls.append)

    with pytest.raises(SourceError):
        WorkdaySource().fetch("acme|wd1|External")
    assert limiter_calls
    assert response.closed


def test_password_field_wins_over_file_input(chromium_page):
    page = chromium_page
    page.set_content(
        '<html><body><input type="password" name="password"><input type="file" name="resume"></body></html>'
    )
    assert _account_wall(page) is True


def test_header_sign_in_chrome_does_not_block_first_step(chromium_page, tmp_path):
    page = chromium_page
    page.set_content(
        """
        <html><body>
          <header><a href="/login">Sign in</a> | <a href="/register">Create account</a></header>
          <form>
            <label for="firstName">First name</label>
            <input id="firstName" name="firstName">
            <label for="lastName">Last name</label>
            <input id="lastName" name="lastName">
            <label for="email">Email</label>
            <input id="email" type="email" name="email">
          </form>
        </body></html>
        """
    )
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")

    report = WorkdayAdapter().fill(page, _job(), _profile(), Preferences(), str(resume), AnswerBank())

    assert ACCOUNT_WALL_NOTE not in " ".join(report.notes)
    assert report.fields
    assert page.input_value("#firstName") == "Ada"


def test_adapter_does_not_match_other_hosts():
    assert not WorkdayAdapter().matches("https://jobs.lever.co/acme/123")


def test_account_wall_returns_note_and_fills_nothing(chromium_page, tmp_path):
    page = chromium_page
    page.set_content(
        """
        <html><body>
          <h1>Create Account</h1>
          <p>Already have an account? Sign In</p>
          <label for="password">Password</label>
          <input id="password" type="password" name="password">
          <button type="submit">Sign In</button>
        </body></html>
        """
    )
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")

    report = WorkdayAdapter().fill(page, _job(), _profile(), Preferences(), str(resume), AnswerBank())

    assert report.ats == "workday"
    assert ACCOUNT_WALL_NOTE in report.notes
    assert report.fields == []
    assert report.resume_uploaded is False
    assert page.input_value("#password") == ""


def test_non_wall_form_fills_common_fields_and_uploads_resume(chromium_page, tmp_path):
    page = chromium_page
    page.set_content(
        """
        <html><body>
          <p>Already have an account? Sign In</p>
          <form>
            <label for="firstName">First name</label>
            <input id="firstName" name="firstName">
            <label for="lastName">Last name</label>
            <input id="lastName" name="lastName">
            <label for="email">Email</label>
            <input id="email" type="email" name="email">
            <label for="phone">Phone</label>
            <input id="phone" name="phone">
            <label for="resume">Resume</label>
            <input id="resume" type="file" name="resume">
          </form>
        </body></html>
        """
    )
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n")

    report = WorkdayAdapter().fill(page, _job(), _profile(), Preferences(), str(resume), AnswerBank())

    assert report.ats == "workday"
    assert report.resume_uploaded is True
    assert ACCOUNT_WALL_NOTE not in report.notes
    assert page.input_value("#firstName") == "Ada"
    assert page.input_value("#lastName") == "Lovelace"
    assert page.input_value("#email") == "ada@example.com"
    assert page.input_value("#phone") == "+1 555 0100"
    labels = {field.label for field in report.fields}
    assert {"First name", "Last name", "Email", "Phone"} <= labels
