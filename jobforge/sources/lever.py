from __future__ import annotations

from typing import Any

from ..models import Job
from .base import (
    JobSource,
    company_from_board,
    html_to_text,
    http_get_json,
    is_remote,
    iso_from_epoch_ms,
    parse_salary,
)


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
            # Lever splits a posting across description, lists, and additional.
            # Reading only descriptionPlain drops the requirements section and
            # makes real postings look like thin/lead-only stubs.
            parts: list[str] = []
            intro = item.get("descriptionPlain") or html_to_text(item.get("description") or "")
            if intro:
                parts.append(str(intro))
            for section in item.get("lists") or []:
                if not isinstance(section, dict):
                    continue
                heading = str(section.get("text") or "").strip()
                if heading:
                    parts.append(heading)
                content = section.get("content") or ""
                content_text = html_to_text(content) if content else ""
                if content_text:
                    parts.append(content_text)
            additional = item.get("additionalPlain") or html_to_text(item.get("additional") or "")
            if additional:
                parts.append(str(additional))
            description = "\n\n".join(part.strip() for part in parts if part.strip())
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
