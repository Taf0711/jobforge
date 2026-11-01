"""Two-stage job discovery: fast scan ranking, then deep fit."""

from .quick import age_days, competition_profile, filter_job, is_us_location, quick_scan
from .registry import fetch_registry, load_registry, registry_path
from .service import dedupe_jobs, scan_jobs

__all__ = [
    "age_days",
    "competition_profile",
    "dedupe_jobs",
    "fetch_registry",
    "filter_job",
    "is_us_location",
    "load_registry",
    "quick_scan",
    "registry_path",
    "scan_jobs",
]
