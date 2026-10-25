"""Fetch and normalize a single public job URL (paste-a-link flow)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..models import Job
from .base import (
    assert_public_url,
    html_to_text,
    http_get,
    is_remote,
    iso_from_string,
    parse_salary,
)

ATS_HOSTS = {
    "greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "jobs.lever.co": "lever",
    "ashbyhq.com": "ashby",
    "jobs.ashbyhq.com": "ashby",
    "workable.com": "workable",
    "apply.workable.com": "workable",
    "smartrecruiters.com": "smartrecruiters",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "myworkdayjobs.com": "workday",
    "myworkdaysite.com": "workday",
    "indeed.com": "indeed",
}


def detect_ats(url: str, html: str = "") -> str:
    host = (urlparse(url).hostname or "").lower()
    for suffix, ats in ATS_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return ats
    haystack = html.lower()
    if "greenhouse.io" in haystack:
        return "greenhouse"
    if "lever.co" in haystack:
        return "lever"
    if "ashbyhq.com" in haystack:
        return "ashby"
    if "workable.com" in haystack:
        return "workable"
    if "myworkdayjobs.com" in haystack:
        return "workday"
    return "generic"


def _json_ld(soup: BeautifulSoup) -> dict[str, Any] | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except (TypeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _location_from_json_ld(item: dict[str, Any]) -> str:
    locations = item.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    parts: list[str] = []
    for loc in locations:
        address = (loc or {}).get("address") or {}
        chunk = ", ".join(
            filter(None, [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")])
        )
        if chunk:
            parts.append(chunk)
    if not parts and item.get("jobLocationType") == "TELECOMMUTE":
        return "Remote"
    return "; ".join(parts)


def _salary_from_json_ld(item: dict[str, Any]) -> tuple[int | None, int | None]:
    salary = item.get("baseSalary") or {}
    value = salary.get("value") if isinstance(salary, dict) else None
    if isinstance(value, dict):
        lo = value.get("minValue") or value.get("value")
        hi = value.get("maxValue") or value.get("value")
        try:
            return (int(lo) if lo else None, int(hi) if hi else None)
        except (TypeError, ValueError):
            pass
    return parse_salary(json.dumps(salary))


def fetch_job_url(url: str) -> Job:
    assert_public_url(url)
    resp = http_get(url, headers={"Accept": "text/html,application/xhtml+xml"})
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    ats = detect_ats(url, html)

    ld = _json_ld(soup)
    if ld:
        title = ld.get("title") or "Untitled"
        company = (ld.get("hiringOrganization") or {}).get("name") or ""
        location = _location_from_json_ld(ld)
        description = html_to_text(ld.get("description") or "")
        smin, smax = _salary_from_json_ld(ld)
        posted = iso_from_string(ld.get("datePosted"))
        apply_url = ld.get("url") or url
        source_id = str((ld.get("identifier") or {}).get("value") or "")
    else:
        title_tag = soup.find("h1") or soup.find("title")
        title = title_tag.get_text(" ", strip=True) if title_tag else "Untitled"
        company_meta = soup.find("meta", attrs={"property": "og:site_name"})
        company = company_meta.get("content", "") if company_meta else ""
        location = ""
        description = html_to_text(soup.get_text("\n"))
        smin, smax = parse_salary(description)
        posted = None
        apply_url = url
        source_id = ""

    if not company:
        host = (urlparse(url).hostname or "").replace("www.", "")
        company = host.split(".")[0].replace("-", " ").title()
    if not source_id:
        source_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

    # Trim boilerplate: keep the densest part of the page if the description is huge.
    description = re.sub(r"\n{3,}", "\n\n", description)[:60_000]

    return Job(
        source=ats if ats != "generic" else "direct",
        source_id=source_id,
        company=company,
        title=title,
        location=location,
        remote=is_remote(location, description),
        salary_min=smin,
        salary_max=smax,
        url=url,
        apply_url=apply_url,
        description=description,
        posted_at=posted,
        raw={"ats": ats},
    )
