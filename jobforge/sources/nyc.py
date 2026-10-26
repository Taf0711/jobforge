"""NYC Open Data "Jobs NYC Postings" source.

Keyless Socrata dataset kpav-sd4t. Every row is a public NYC government
posting with a description, salary range, and a cityjobs.nyc.gov job id.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from ..models import Job
from .base import JobSource, http_get_json, is_remote, iso_from_string

API = "https://data.cityofnewyork.us/resource/kpav-sd4t.json"
JOB_URL = "https://cityjobs.nyc.gov/job/{job_id}"


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


class NYCOpenDataSource(JobSource):
    name = "nyc_opendata"

    def fetch(self, board: str = "", **kwargs: Any) -> list[Job]:
        limit = max(1, min(int(kwargs.get("limit") or 200), 1000))
        params: dict[str, Any] = {"$limit": limit, "$order": "posting_date DESC"}
        query = str(kwargs.get("query") or "").strip()
        if query:
            # Full-text search is needed: the latest 200 rows are mostly
            # non-technical city titles and would never reach the title gate.
            params["$q"] = query
        url = f"{API}?{urlencode(params)}"
        rows = http_get_json(url)
        if not isinstance(rows, list):
            return []

        jobs: list[Job] = []
        seen_ids: set[str] = set()
        for item in rows:
            job_id = str(item.get("job_id") or "").strip()
            if job_id and job_id in seen_ids:
                continue
            seen_ids.add(job_id)
            title = (
                item.get("business_title")
                or item.get("civil_service_title")
                or "Untitled"
            )
            location = str(item.get("work_location") or "")
            description = "\n\n".join(
                str(value).strip()
                for value in (
                    item.get("job_description"),
                    item.get("minimum_qual_requirements"),
                    item.get("preferred_skills"),
                )
                if value
            )
            link = JOB_URL.format(job_id=job_id) if job_id else ""
            jobs.append(
                Job(
                    source=self.name,
                    source_id=job_id or str(item.get("job_id") or title),
                    company=str(item.get("agency") or "NYC"),
                    title=str(title),
                    location=location,
                    remote=is_remote(location, description),
                    salary_min=_money(item.get("salary_range_from")),
                    salary_max=_money(item.get("salary_range_to")),
                    url=link,
                    apply_url=link,
                    description=description,
                    posted_at=iso_from_string(item.get("posting_date")),
                    raw=item,
                )
            )
        return jobs
