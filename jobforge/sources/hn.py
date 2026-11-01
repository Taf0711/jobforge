"""Hacker News "Who is Hiring" source.

Uses the public Algolia API to find the latest whoishiring thread and read its
top-level comments. Comments are free text, so company/title/location parsing is
best-effort and marked as such in the raw record.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from ..models import Job
from .base import JobSource, http_get_json, is_remote

SEARCH_URL = "https://hn.algolia.com/api/v1/search"
ITEM_URL = "https://news.ycombinator.com/item?id={}"


def _latest_thread() -> dict[str, Any] | None:
    query = quote("Ask HN: Who is hiring?")
    data = http_get_json(
        f"{SEARCH_URL}?query={query}&tags=story,author_whoishiring&hitsPerPage=5",
        timeout=20,
    )
    hits = data.get("hits") or []
    for hit in hits:
        title = str(hit.get("title") or "").lower()
        if "who is hiring" in title and "freelancer" not in title:
            return hit
    return hits[0] if hits else None


def _comment_rows(story_id: Any, limit: int) -> list[dict[str, Any]]:
    data = http_get_json(
        f"{SEARCH_URL}?tags=comment,story_{story_id}&hitsPerPage={max(1, min(limit, 200))}",
        timeout=20,
    )
    return [hit for hit in (data.get("hits") or []) if isinstance(hit, dict)]


def _split_comment(text: str) -> tuple[str, str, str]:
    lines = [line.strip() for line in re.split(r"<[^>]+>|\n", text or "") if line.strip()]
    if not lines:
        return "", "", ""
    first = lines[0]
    parts = [part.strip() for part in re.split(r"\s*\|\s*", first) if part.strip()]
    company = parts[0] if parts else first
    title = parts[1] if len(parts) > 1 else ""
    location = ""
    for part in parts[1:]:
        lowered = part.lower()
        if any(token in lowered for token in ("remote", "nyc", "new york", "sf", "san francisco", "seattle", "austin", "boston", "london", "toronto", "usa", "us")):
            location = part
            break
    if not title:
        title = first
    return company[:120], title[:160], location[:120]


class HNSource(JobSource):
    name = "hn"

    def fetch(self, board: str = "", **kwargs: Any) -> list[Job]:
        months = int(kwargs.get("months") or 2)
        limit = int(kwargs.get("limit") or 200)
        thread = _latest_thread()
        if not thread:
            return []
        story_id = thread.get("objectID")
        created = thread.get("created_at")
        posted_at = None
        if created:
            try:
                posted_at = datetime.fromisoformat(str(created).replace("Z", "+00:00")).isoformat(timespec="seconds")
            except ValueError:
                posted_at = str(created)
        jobs: list[Job] = []
        for comment in _comment_rows(story_id, limit):
            text = str(comment.get("comment_text") or "")
            company, title, location = _split_comment(text)
            if not company or not title:
                continue
            comment_id = comment.get("objectID")
            url = ITEM_URL.format(comment_id)
            description = re.sub(r"<[^>]+>", " ", text)
            description = re.sub(r"\s+", " ", description).strip()
            jobs.append(
                Job(
                    source=self.name,
                    source_id=str(comment_id),
                    company=company,
                    title=title,
                    location=location,
                    remote=is_remote(location, description),
                    url=url,
                    apply_url=url,
                    description=description[:12000],
                    posted_at=posted_at,
                    raw={
                        "story_id": story_id,
                        "story_title": thread.get("title"),
                        "author": comment.get("author"),
                        "months": months,
                        "parse": "best-effort free text",
                    },
                )
            )
        return jobs
