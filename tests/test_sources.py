from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

from jobforge.models import Job
from jobforge.sources.base import (
    MAX_BYTES,
    _retry_after_seconds,
    html_to_text,
    is_remote,
    parse_salary,
)
from jobforge.sources.direct import detect_ats


def test_salary_parsing():
    assert parse_salary("$120,000 - $160,000") == (120000, 160000)
    assert parse_salary("$120K-$160K") == (120000, 160000)
    assert parse_salary("No salary listed") == (None, None)
    assert parse_salary("$90,000") == (90000, 90000)


def test_html_and_remote():
    assert "Hello world" in html_to_text("<div><p>Hello <b>world</b></p><script>x</script></div>")
    assert is_remote("Remote (US)")
    assert is_remote("San Francisco", "This role is remote-friendly")
    assert not is_remote("New York, NY")


def test_fingerprint_is_stable():
    a = Job(source="lever", source_id="1", company="Acme", title="Engineer", apply_url="https://x/1")
    b = Job(source="greenhouse", source_id="2", company="Acme", title="Engineer", apply_url="https://x/1")
    assert a.fingerprint == b.fingerprint


def test_retry_after_accepts_http_date():
    future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    delay = _retry_after_seconds(future)
    assert 0 < delay <= 31
    assert _retry_after_seconds("3") == 3.0
    assert _retry_after_seconds("not-a-date") == 2.0


def test_large_greenhouse_boards_fit_response_cap():
    # Stripe/Anthropic/Databricks exceed 5 MB with content=true; the old cap
    # silently dropped the largest and most useful boards.
    assert MAX_BYTES >= 16 * 1024 * 1024


def test_ats_detection():
    assert detect_ats("https://jobs.lever.co/acme/123") == "lever"
    assert detect_ats("https://job-boards.greenhouse.io/acme/jobs/1") == "greenhouse"
    assert detect_ats("https://jobs.ashbyhq.com/acme/123") == "ashby"
    assert detect_ats("https://apply.workable.com/acme/j/ABC/") == "workable"
    assert detect_ats("https://example.com/jobs/1") == "generic"
