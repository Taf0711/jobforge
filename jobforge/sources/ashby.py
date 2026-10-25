from __future__ import annotations

import json
from typing import Any

from ..models import Job
from .base import JobSource, company_from_board, http_get_json, is_remote, iso_from_string, parse_salary


class AshbySource(JobSource):
    name = "ashby"

    def fetch(self, board: str, **kwargs: Any) -> list[Job]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true"
        data = http_get_json(url)
        jobs: list[Job] = []
        for item in data.get("jobs", []):
            location = item.get("location") or ""
            description = item.get("descriptionPlain") or item.get("descriptionHtml") or ""
            comp = item.get("compensation") or {}
            salary_text = " ".join(
                str(comp.get(key) or "")
                for key in ("compensationTierSummary", "scrapeableCompensationSalarySummary")
            )
            if not salary_text.strip():
                salary_text = json.dumps(comp)
            smin, smax = parse_salary(salary_text)
            if smin is None:
                smin, smax = parse_salary(description)
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id")),
                    company=company_from_board(board),
                    title=item.get("title") or "Untitled",
                    location=location,
                    remote=is_remote(location, description, item.get("isRemote") or item.get("workplaceType") == "Remote"),
                    salary_min=smin,
                    salary_max=smax,
                    url=item.get("jobUrl") or "",
                    apply_url=item.get("applyUrl") or item.get("jobUrl") or "",
                    description=description,
                    posted_at=iso_from_string(item.get("publishedAt")),
                    raw=item,
                )
            )
        return jobs
