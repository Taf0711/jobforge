"""USAJobs public Search API source.

Requires a free API key from https://developer.usajobs.gov/:
  export USAJOBS_API_KEY=...
  export USAJOBS_EMAIL=you@example.com

Without both variables the source returns an empty list (it is optional, not an
error). Only public postings are read; no account or authenticated API is used.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlencode

from ..models import Job
from .base import JobSource, html_to_text, http_get_json, is_remote, iso_from_string

API = "https://data.usajobs.gov/api/search"


def _money(value: Any) -> int | None:
    if value is None:
        return None
    digits = re.sub(r"[^0-9.]", "", str(value))
    if not digits:
        return None
    try:
        return int(round(float(digits)))
    except ValueError:
        return None


class USAJobsSource(JobSource):
    name = "usajobs"

    def fetch(self, board: str = "", **kwargs: Any) -> list[Job]:
        key = os.environ.get("USAJOBS_API_KEY", "").strip()
        email = (
            os.environ.get("USAJOBS_EMAIL")
            or os.environ.get("USAJOBS_USER_AGENT_EMAIL")
            or ""
        ).strip()
        if not key or not email:
            return []

        keyword = str(kwargs.get("keyword") or "software engineer")
        limit = max(1, min(int(kwargs.get("limit") or 50), 500))
        params: dict[str, Any] = {"Keyword": keyword, "ResultsPerPage": limit}
        location = str(kwargs.get("location") or "").strip()
        if location:
            params["LocationName"] = location

        data = http_get_json(
            f"{API}?{urlencode(params)}",
            headers={
                "Authorization-Key": key,
                "User-Agent": email,
                "Accept": "application/json",
            },
        )
        items = (data.get("SearchResult") or {}).get("SearchResultItems") or []
        jobs: list[Job] = []
        for item in items:
            descriptor = item.get("MatchedObjectDescriptor") or {}
            title = descriptor.get("PositionTitle") or "Untitled"
            locations = descriptor.get("PositionLocation") or []
            location_name = (
                str(locations[0].get("LocationName") or "") if locations else ""
            )
            details = (descriptor.get("UserArea") or {}).get("Details") or {}
            description = "\n\n".join(
                html_to_text(str(value))
                for value in (
                    details.get("JobSummary"),
                    details.get("QualificationSummary"),
                    details.get("Requirements"),
                )
                if value
            )
            position_uri = str(descriptor.get("PositionURI") or "")
            apply_uris = descriptor.get("ApplyURI") or []
            if isinstance(apply_uris, str):
                apply_url = apply_uris
            elif isinstance(apply_uris, list) and apply_uris:
                apply_url = str(apply_uris[0])
            else:
                apply_url = position_uri

            remuneration = descriptor.get("PositionRemuneration") or []
            salary = remuneration[0] if remuneration else {}
            salary_min = _money(salary.get("MinimumRange"))
            salary_max = _money(salary.get("MaximumRange"))

            position_id = str(
                descriptor.get("PositionID")
                or descriptor.get("PositionURI")
                or f"{descriptor.get('OrganizationName', '')}:{title}"
            )
            jobs.append(
                Job(
                    source=self.name,
                    source_id=position_id,
                    company=descriptor.get("DepartmentName")
                    or descriptor.get("OrganizationName")
                    or "US Federal",
                    title=title,
                    location=location_name,
                    remote=is_remote(location_name, description),
                    salary_min=salary_min,
                    salary_max=salary_max,
                    url=position_uri,
                    apply_url=apply_url,
                    description=description,
                    posted_at=iso_from_string(
                        descriptor.get("PublicationStartDate")
                        or details.get("PublicationStartDate")
                    ),
                    raw=descriptor,
                )
            )
        return jobs
