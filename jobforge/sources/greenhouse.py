from __future__ import annotations

import html
from typing import Any

from ..models import Job
from .base import JobSource, company_from_board, html_to_text, http_get_json, is_remote, iso_from_string, parse_salary


class GreenhouseSource(JobSource):
    name = "greenhouse"

    def fetch(self, board: str, **kwargs: Any) -> list[Job]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs?content=true"
        data = http_get_json(url)
        jobs: list[Job] = []
        for item in data.get("jobs", []):
            location = (item.get("location") or {}).get("name", "") or ""
            description = html_to_text(html.unescape(item.get("content") or ""))
            company = item.get("company_name") or company_from_board(board)
            salary_text = ""
            for meta in item.get("metadata") or []:
                name = str(meta.get("name", "")).lower()
                if any(token in name for token in ("salary", "compensation", "pay range")):
                    value = meta.get("value")
                    salary_text = " ".join(value) if isinstance(value, list) else str(value or "")
                    break
            smin, smax = parse_salary(salary_text)
            if smin is None:
                smin, smax = parse_salary(description)
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(item.get("id")),
                    company=company,
                    title=item.get("title") or "Untitled",
                    location=location,
                    remote=is_remote(location, description),
                    salary_min=smin,
                    salary_max=smax,
                    url=item.get("absolute_url") or "",
                    apply_url=item.get("absolute_url") or "",
                    description=description,
                    posted_at=iso_from_string(item.get("first_published") or item.get("updated_at")),
                    raw=item,
                )
            )
        return jobs
