"""Flat scan-source registry support.

The resume-forge scanner uses a flat ``scan_sources.yml`` list with one entry per
board/source and an explicit ``competition_profile``. JobForge keeps its older
grouped ``sources:`` config, but this module lets the scanner use the flat
registry when it is present.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import yaml

from ..config import Config
from ..models import Job
from ..sources.ashby import AshbySource
from ..sources.base import SourceError
from ..sources.github_jobs import GitHubPipeSource, GitHubJobsSource
from ..sources.greenhouse import GreenhouseSource
from ..sources.hn import HNSource
from ..sources.lever import LeverSource
from ..sources.nyc import NYCOpenDataSource
from ..sources.registry import FetchReport
from ..sources.usajobs import USAJobsSource
from ..sources.workable import WorkableSource
from ..sources.workday import WorkdaySource
from .quick import COMPETITION_DEFAULT


def registry_path(config: Config) -> Path | None:
    """Return the configured flat registry path, if it exists."""
    scan = config.scan or {}
    raw = str(scan.get("sources_path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(config.config_path).expanduser().parent / path if config.config_path else path
    return path if path.is_file() else None


def load_registry(path: str | Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    entries = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    return [dict(item) for item in entries if isinstance(item, dict)]


def _repo_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.hostname not in {"github.com", "raw.githubusercontent.com"}:
        raise ValueError(f"Not a GitHub URL: {url}")
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname == "raw.githubusercontent.com":
        if len(parts) < 2:
            raise ValueError(f"Cannot find owner/repo in {url}")
        return f"{parts[0]}/{parts[1]}"
    if len(parts) < 2:
        raise ValueError(f"Cannot find owner/repo in {url}")
    return f"{parts[0]}/{parts[1].removesuffix('.git')}"


def _workday_board(entry: dict[str, Any]) -> str:
    tenant = str(entry.get("tenant") or "").strip()
    site = str(entry.get("site") or "").strip()
    host = str(entry.get("host") or "").strip()
    if not tenant or not site:
        raise ValueError("Workday entry needs tenant and site")
    if host:
        return f"https://{host}/{site}"
    wd = str(entry.get("wd") or "wd1").strip()
    return f"{tenant}|{wd}|{site}"


def _source_for(entry: dict[str, Any]) -> tuple[Any, str, dict[str, Any], str]:
    """Return (source, board, kwargs, competition_profile) for one registry entry."""
    kind = str(entry.get("type") or "").strip().lower()
    name = str(entry.get("name") or kind or "source").strip()
    competition = str(entry.get("competition_profile") or COMPETITION_DEFAULT.get(kind, "mass_aggregator"))
    kwargs: dict[str, Any] = {
        key: value for key, value in entry.items()
        if key not in {"name", "type", "board", "site", "tenant", "host", "wd", "url", "enabled", "competition_profile", "notes"}
    }
    if kind == "greenhouse":
        return GreenhouseSource(), str(entry.get("board") or ""), kwargs, competition
    if kind == "lever":
        return LeverSource(), str(entry.get("site") or entry.get("board") or ""), kwargs, competition
    if kind == "ashby":
        return AshbySource(), str(entry.get("board") or ""), kwargs, competition
    if kind == "workable":
        return WorkableSource(), str(entry.get("board") or ""), kwargs, competition
    if kind == "workday":
        kwargs.setdefault("max_jobs", int(entry.get("limit") or 100))
        return WorkdaySource(), _workday_board(entry), kwargs, competition
    if kind == "github":
        return GitHubJobsSource(), _repo_from_url(str(entry.get("url") or "")), kwargs, competition
    if kind == "github_pipe":
        return GitHubPipeSource(), str(entry.get("url") or ""), {"name": name}, competition
    if kind == "hn":
        return HNSource(), "", kwargs, competition
    if kind == "usajobs":
        return USAJobsSource(), "", kwargs, competition
    if kind == "nyc_opendata":
        return NYCOpenDataSource(), "", kwargs, competition
    if kind == "manual":
        raise SourceError("manual source")
    raise SourceError(f"unsupported registry type: {kind or 'unknown'}")


def fetch_registry(config: Config, only: Iterable[str] | None = None) -> FetchReport:
    """Fetch every enabled entry in the flat registry."""
    report = FetchReport()
    path = registry_path(config)
    if path is None:
        return report
    if isinstance(only, str):
        only = [only]
    selected = {str(item).strip().lower() for item in (only or []) if str(item).strip()}
    for entry in load_registry(path):
        if entry.get("enabled") is False:
            continue
        name = str(entry.get("name") or entry.get("type") or "").strip()
        kind = str(entry.get("type") or "").strip().lower()
        aliases = {name.lower(), kind.lower()}
        if kind == "github":
            aliases.add("github_jobs")
        if kind == "github_pipe":
            aliases.add("github_pipe")
        if selected and not (aliases & selected):
            continue
        try:
            source, board, kwargs, competition = _source_for(entry)
            jobs = source.fetch(board, **kwargs)
        except SourceError as exc:
            if str(exc) == "manual source":
                continue
            report.errors.append(f"{name or kind}: {exc}")
            continue
        except Exception as exc:  # one source must not abort the scan
            report.errors.append(f"{name or kind}: {exc}")
            continue
        for job in jobs:
            job.competition_profile = competition
        report.jobs.extend(jobs)
        report.per_source[name or kind] = report.per_source.get(name or kind, 0) + len(jobs)
    return report


def default_competition(job: Job) -> str:
    return job.competition_profile or COMPETITION_DEFAULT.get(job.source, "mass_aggregator")
