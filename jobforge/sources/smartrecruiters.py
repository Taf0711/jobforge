from __future__ import annotations

from typing import Any

from ..models import Job
from .base import JobSource, company_from_board, html_to_text, http_get_json, is_remote, iso_from_string, parse_salary


class SmartRecruitersSource(JobSource):
    name = "smartrecruiters"

    def fetch(self, board: str, **kwargs: Any) -> list[Job]:
        url = f"https://api.smartrecruiters.com/v1/companies/{board}/postings?limit=100"
        data = http_get_json(url)
        jobs: list[Job] = []
        for item in data.get("content", []):
            loc = item.get("location") or {}
            location = ", ".join(filter(None, [loc.get("city"), loc.get("region"), loc.get("country")]))
            company = (item.get("company") or {}).get("name") or company_from_board(board)
            description = html_to_text(item.get("jobAd") or "") if isinstance(item.get("jobAd"), str) else ""
            smin, smax = parse_salary(description)
            job_id = item.get("id") or item.get("uuid")
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(job_id),
                    company=company,
                    title=item.get("name") or "Untitled",
                    location=location,
                    remote=is_remote(location, description),
                    salary_min=smin,
                    salary_max=smax,
                    url=item.get("postingUrl") or f"https://jobs.smartrecruiters.com/{board}/{job_id}",
                    apply_url=item.get("applyingUrl") or item.get("postingUrl") or f"https://jobs.smartrecruiters.com/{board}/{job_id}",
                    description=description,
                    posted_at=iso_from_string(item.get("releasedDate")),
                    raw=item,
                )
            )
        return jobs
