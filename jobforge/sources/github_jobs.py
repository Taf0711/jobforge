"""Public GitHub job-list sources.

Repositories such as SimplifyJobs/New-Grad-Positions publish community-maintained
tables of employer postings. They are useful for discovery, but the table often
contains a Simplify redirect alongside the employer link. JobForge keeps only a
direct employer/ATS URL so the quality gate still has a canonical application
destination. Rows with no direct link are skipped, not silently applied through
an aggregator.

These are lead sources: the README does not contain the full job description.
The employer posting is opened for the real requirements before applying.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ..models import Job
from .base import JobSource, http_get, is_remote

AGGREGATOR_HOSTS = (
    "simplify.jobs",
    "swelist.com",
    "jobright.ai",
    "hiring.cafe",
    "linkedin.com",
    "indeed.com",
)
TRACKING_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "gh_src", "source",
}
MAX_ROWS = 1500
AGE = re.compile(r"(\d+)\s*(h|d|w|mo|y)", re.IGNORECASE)
MARKDOWN_LINK = re.compile(r"\]\((https?://[^)\s]+)\)")
BARE_URL = re.compile(r"https?://[^\s|)]+")
EMOJI = re.compile(
    "[\U0001F1E6-\U0001F1FF\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D\u21B3]+"
)


def _repo_parts(board: str) -> tuple[str, str]:
    value = (board or "").strip().rstrip("/")
    if value.startswith(("https://github.com/", "http://github.com/")):
        value = value.split("github.com/", 1)[1]
    elif value.startswith("github.com/"):
        value = value.split("github.com/", 1)[1]
    value = value.removesuffix(".git")
    parts = [part for part in value.split("/") if part]
    if len(parts) < 2:
        raise ValueError("GitHub job source must be owner/repo")
    owner, repo = parts[0], parts[1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid GitHub owner/repo")
    return owner, repo


def _strip_tracking(url: str) -> str:
    parsed = urlsplit(html.unescape(url.strip()))
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_KEYS
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _is_direct_apply(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    return not any(host == blocked or host.endswith("." + blocked) for blocked in AGGREGATOR_HOSTS)


def _direct_url(urls: list[str]) -> str:
    for raw in urls:
        url = _strip_tracking(raw)
        if _is_direct_apply(url):
            return url
    return ""


def _age_days(value: str) -> int | None:
    match = AGE.search(value or "")
    if not match:
        return None
    number = int(match.group(1))
    unit = match.group(2).lower()
    return number * {"h": 0, "d": 1, "w": 7, "mo": 30, "y": 365}[unit]


def _posted_at(value: str) -> str | None:
    days = _age_days(value)
    if days is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def _company_text(value: str) -> str:
    text = EMOJI.sub(" ", value or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text.strip(" -–—:;,.")


def _column(headers: list[str], names: tuple[str, ...], default: int | None) -> int | None:
    for index, header in enumerate(headers):
        if any(name in header for name in names):
            return index
    return default


def _row(
    cells: list[str],
    links: list[list[str]],
    headers: list[str],
    *,
    repo: str,
    list_url: str,
    previous_company: str = "",
) -> Job | None:
    if len(cells) < 3:
        return None
    company_i = _column(headers, ("company", "employer"), 0)
    title_i = _column(headers, ("role", "title", "position", "job"), 1)
    location_i = _column(headers, ("location", "city", "place"), 2)
    apply_i = _column(headers, ("application", "apply", "link"), 3)
    age_i = _column(headers, ("age", "date", "posted"), 4)
    company = _company_text(cells[company_i]) if company_i is not None and company_i < len(cells) else ""
    if not company or company in {"-", "—", "->", "→"}:
        company = previous_company
    title = cells[title_i].strip() if title_i is not None and title_i < len(cells) else ""
    location = cells[location_i].strip() if location_i is not None and location_i < len(cells) else ""
    age = cells[age_i].strip() if age_i is not None and age_i < len(cells) else ""
    apply_urls = links[apply_i] if apply_i is not None and apply_i < len(links) else []
    apply_url = _direct_url(apply_urls)
    if not company or not title or not apply_url:
        return None
    simplify_url = next(
        (url for url in apply_urls if "simplify.jobs" in (urlsplit(url).hostname or "")),
        "",
    )
    ident = hashlib.sha256(
        f"{repo}|{company}|{title}|{location}|{apply_url}".encode("utf-8")
    ).hexdigest()[:32]
    description = (
        f"{title}\n{company}" + (f" — {location}" if location else "") +
        f"\nCommunity job list: {repo}."
    )
    if age:
        description += f" Listed {age} ago."
    description += " Open the employer posting for the full description."
    return Job(
        source="github_jobs",
        source_id=ident,
        company=company,
        title=title,
        location=location,
        remote=is_remote(location, ""),
        url=apply_url,
        apply_url=apply_url,
        description=description,
        posted_at=_posted_at(age),
        raw={
            "repo": repo,
            "list_url": list_url,
            "age": age,
            "apply_url": apply_url,
            "simplify_url": simplify_url,
            "row": cells,
        },
    )


def _html_rows(text: str, repo: str, list_url: str) -> list[Job]:
    soup = BeautifulSoup(text, "html.parser")
    jobs: list[Job] = []
    last_company = ""
    for table in soup.find_all("table"):
        headers = [
            th.get_text(" ", strip=True).lower()
            for th in table.find_all("th")
        ]
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            cells = [td.get_text(" ", strip=True) for td in tds]
            links = [
                [a.get("href", "") for a in td.find_all("a", href=True)]
                for td in tds
            ]
            job = _row(
                cells, links, headers, repo=repo, list_url=list_url,
                previous_company=last_company,
            )
            if job:
                jobs.append(job)
                last_company = job.company
            if len(jobs) >= MAX_ROWS:
                return jobs
    return jobs


def _markdown_rows(text: str, repo: str, list_url: str) -> list[Job]:
    jobs: list[Job] = []
    headers: list[str] | None = None
    last_company = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not (line.startswith("|") and line.endswith("|")):
            headers = None
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if headers is None:
            headers = [cell.lower() for cell in cells]
            continue
        if all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells):
            continue
        links = []
        for cell in cells:
            urls = MARKDOWN_LINK.findall(cell)
            urls.extend(BARE_URL.findall(cell))
            links.append(urls)
        job = _row(
            cells, links, headers, repo=repo, list_url=list_url,
            previous_company=last_company,
        )
        if job:
            jobs.append(job)
            last_company = job.company
        if len(jobs) >= MAX_ROWS:
            break
    return jobs


class GitHubJobsSource(JobSource):
    name = "github_jobs"

    def fetch(self, board: str, **kwargs: Any) -> list[Job]:
        owner, repo = _repo_parts(board)
        full_name = f"{owner}/{repo}"
        list_url = f"https://github.com/{full_name}"
        readme_url = (
            f"https://raw.githubusercontent.com/{quote(owner)}/{quote(repo)}/"
            "HEAD/README.md"
        )
        response = http_get(readme_url, max_bytes=8 * 1024 * 1024)
        text = response.content.decode("utf-8", errors="replace")
        jobs = _html_rows(text, full_name, list_url)
        if not jobs:
            jobs = _markdown_rows(text, full_name, list_url)
        seen: set[tuple[str, str, str, str]] = set()
        unique: list[Job] = []
        for job in jobs:
            key = (job.company.lower(), job.title.lower(), job.location.lower(), job.apply_url.lower())
            if key in seen:
                continue
            seen.add(key)
            unique.append(job)
        return unique[:MAX_ROWS]
