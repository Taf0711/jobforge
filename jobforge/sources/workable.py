from __future__ import annotations

from typing import Any

from ..models import Job
from .base import JobSource, company_from_board, html_to_text, http_get_json, is_remote, iso_from_string, parse_salary


class WorkableSource(JobSource):
    name = "workable"

    def fetch(self, board: str, **kwargs: Any) -> list[Job]:
        url = f"https://apply.workable.com/api/v1/widget/accounts/{board}?details=true"
        data = http_get_json(url)
        company = data.get("name") or company_from_board(board)
        jobs: list[Job] = []
        for item in data.get("jobs", []):
            locations = item.get("locations") or []
            if locations:
                location = "; ".join(
                    ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
                    for loc in locations
                )
            else:
                location = ", ".join(filter(None, [item.get("city"), item.get("state"), item.get("country")]))
            description = html_to_text(item.get("description") or "")
            smin, smax = parse_salary(description)
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("shortcode") or item.get("id") or ""),
                    company=company,
                    title=item.get("title") or "Untitled",
                    location=location,
                    remote=is_remote(location, description, item.get("telecommuting")),
                    salary_min=smin,
                    salary_max=smax,
                    url=item.get("url") or item.get("shortlink") or "",
                    apply_url=item.get("application_url") or item.get("url") or "",
                    description=description,
                    posted_at=iso_from_string(item.get("published_on")),
                    raw=item,
                )
            )
        return jobs
