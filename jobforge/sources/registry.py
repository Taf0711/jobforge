from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..models import Job
from .ashby import AshbySource
from .base import JobSource, SourceError
from .direct import fetch_job_url
from .github_jobs import GitHubJobsSource
from .greenhouse import GreenhouseSource
from .lever import LeverSource
from .nyc import NYCOpenDataSource
from .smartrecruiters import SmartRecruitersSource
from .usajobs import USAJobsSource
from .workable import WorkableSource
from .workday import WorkdaySource

SOURCES: dict[str, JobSource] = {
    "greenhouse": GreenhouseSource(),
    "lever": LeverSource(),
    "ashby": AshbySource(),
    "workable": WorkableSource(),
    "smartrecruiters": SmartRecruitersSource(),
    "workday": WorkdaySource(),
    "nyc_opendata": NYCOpenDataSource(),
    "usajobs": USAJobsSource(),
    "github_jobs": GitHubJobsSource(),
}

# Sources configured as one section object, not a list of board slugs.
SINGLETON_SOURCES = {"nyc_opendata", "usajobs"}


@dataclass
class FetchReport:
    jobs: list[Job] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    per_source: dict[str, int] = field(default_factory=dict)


def get_source(name: str) -> JobSource:
    if name not in SOURCES:
        raise SourceError(f"Unknown source '{name}'. Available: {', '.join(sorted(SOURCES))}")
    return SOURCES[name]


def configured_boards(config: Config, name: str) -> list[str]:
    section = config.sources.get(name) or {}
    if not isinstance(section, dict):
        return []
    boards = section.get("boards") or section.get("companies") or section.get("repos") or []
    return [str(b) for b in boards if str(b).strip()]


BOARD_SELECTOR = re.compile(
    r"^(greenhouse|lever|ashby|workable|smartrecruiters):"
    r"([A-Za-z0-9][A-Za-z0-9._-]{0,100})$",
    re.IGNORECASE,
)


def parse_board_selector(value: str) -> tuple[str, str] | None:
    """Parse an explicit known-ATS board selector.

    This is deliberately narrower than arbitrary URL ingestion: it can only
    name a board slug on an ATS adapter JobForge already implements.
    """
    match = BOARD_SELECTOR.fullmatch(value.strip())
    if not match:
        return None
    return match.group(1).lower(), match.group(2)


def _fetch_board(
    report: FetchReport, name: str, board: str
) -> None:
    if name not in SOURCES:
        report.errors.append(f"Unknown source: {name}")
        return
    if name in SINGLETON_SOURCES:
        report.errors.append(f"Source '{name}' does not take a board selector")
        return
    try:
        jobs = SOURCES[name].fetch(board)
        report.jobs.extend(jobs)
        report.per_source[f"{name}:{board}"] = (
            report.per_source.get(f"{name}:{board}", 0) + len(jobs)
        )
    except Exception as exc:  # one bad board should not abort the scan
        report.errors.append(f"{name}/{board}: {exc}")


def fetch_configured(config: Config, only: list[str] | None = None) -> FetchReport:
    report = FetchReport()
    if isinstance(only, str):
        only = [only]
    selectors = list(dict.fromkeys(str(item).strip() for item in (only or []) if str(item).strip()))
    plain: list[str] = []
    boards: list[tuple[str, str]] = []
    for selector in selectors:
        parsed = parse_board_selector(selector)
        if parsed is None:
            plain.append(selector)
        else:
            boards.append(parsed)

    for name in (plain or ([] if selectors else list(SOURCES))):
        if name not in SOURCES:
            report.errors.append(f"Unknown source: {name}")
            continue
        section = config.sources.get(name)
        if name in SINGLETON_SOURCES:
            if not isinstance(section, dict) or section.get("enabled", True) is False:
                continue
            try:
                jobs = SOURCES[name].fetch("", **section)
                report.jobs.extend(jobs)
                report.per_source[name] = report.per_source.get(name, 0) + len(jobs)
            except Exception as exc:  # one optional source must not abort the scan
                report.errors.append(f"{name}: {exc}")
            continue
        for board in configured_boards(config, name):
            try:
                jobs = SOURCES[name].fetch(board)
                report.jobs.extend(jobs)
                report.per_source[name] = report.per_source.get(name, 0) + len(jobs)
            except Exception as exc:  # one bad board should not abort the scan
                report.errors.append(f"{name}/{board}: {exc}")

    for name, board in boards:
        _fetch_board(report, name, board)
    return report


def fetch_url(url: str) -> Job:
    return fetch_job_url(url)
