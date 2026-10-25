from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..config import Config
from ..models import Job
from .ashby import AshbySource
from .base import JobSource, SourceError
from .direct import fetch_job_url
from .greenhouse import GreenhouseSource
from .lever import LeverSource
from .smartrecruiters import SmartRecruitersSource
from .workable import WorkableSource
from .workday import WorkdaySource

SOURCES: dict[str, JobSource] = {
    "greenhouse": GreenhouseSource(),
    "lever": LeverSource(),
    "ashby": AshbySource(),
    "workable": WorkableSource(),
    "smartrecruiters": SmartRecruitersSource(),
    "workday": WorkdaySource(),
}


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
    boards = section.get("boards") or section.get("companies") or []
    return [str(b) for b in boards if str(b).strip()]


def fetch_configured(config: Config, only: list[str] | None = None) -> FetchReport:
    report = FetchReport()
    names = only or list(SOURCES)
    for name in names:
        if name not in SOURCES:
            report.errors.append(f"Unknown source: {name}")
            continue
        for board in configured_boards(config, name):
            try:
                jobs = SOURCES[name].fetch(board)
                report.jobs.extend(jobs)
                report.per_source[name] = report.per_source.get(name, 0) + len(jobs)
            except Exception as exc:  # one bad board should not abort the scan
                report.errors.append(f"{name}/{board}: {exc}")
    return report


def fetch_url(url: str) -> Job:
    return fetch_job_url(url)
