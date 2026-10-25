from __future__ import annotations

from typing import Any

from ..models import Job
from .base import JobSource, company_from_board, http_get_json, is_remote, iso_from_epoch_ms, parse_salary


class LeverSource(JobSource):
    name = "lever"

    def fetch(self, board: str, **kwargs: Any) -> list[Job]:
        url = f"https://api.lever.co/v0/postings/{board}?mode=json"
        data = http_get_json(url)
        if not isinstance(data, list):
            return []
        jobs: list[Job] = []
        for item in data:
            categories = item.get("categories") or {}
            location = categories.get("location") or item.get("country") or ""
            description = item.get("descriptionPlain") or item.get("description") or ""
            salary = item.get("salaryRange") or {}
            smin = salary.get("min")
            smax = salary.get("max")
            if smin is None and smax is None:
                smin, smax = parse_salary(description)
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id")),
                    company=company_from_board(board),
                    title=item.get("text") or "Untitled",
                    location=location,
                    remote=is_remote(location, description, item.get("workplaceType") == "remote"),
                    salary_min=int(smin) if smin else None,
                    salary_max=int(smax) if smax else None,
                    url=item.get("hostedUrl") or "",
                    apply_url=item.get("applyUrl") or item.get("hostedUrl") or "",
                    description=description,
                    posted_at=iso_from_epoch_ms(item.get("createdAt")),
                    raw=item,
                )
            )
        return jobs
