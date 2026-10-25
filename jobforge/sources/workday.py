"""Workday job discovery through the public CXS endpoint.

A Workday board is addressed as ``tenant|wd1|site``, ``tenant|site`` (which
defaults to ``wd1``), or a public ``myworkdayjobs.com`` board URL. Only public
postings are read. Authentication, account creation, and employer credentials
remain out of scope.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import requests

from ..models import Job
from .base import (
    MAX_BYTES,
    USER_AGENT,
    JobSource,
    SourceError,
    _LIMITER,
    company_from_board,
    html_to_text,
    http_get_json,
    is_remote,
)

DEFAULT_WD = "wd1"
TIMEOUT = 30
RETRIES = 2

_BOARD_HELP = "Use 'tenant|wd1|site', 'tenant|site', or a myworkdayjobs.com board URL"
_WORKDAY_HOST = re.compile(r"^(?P<tenant>[a-z0-9][a-z0-9-]*)\.(?P<wd>wd\d+)\.myworkdayjobs\.com$", re.IGNORECASE)
_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]*$", re.IGNORECASE)
_WD = re.compile(r"^wd\d+$", re.IGNORECASE)
_POSTED_FORMATS = ("%m/%d/%Y", "%Y/%m/%d", "%B %d, %Y", "%b %d, %Y")


def parse_board(board: str) -> tuple[str, str, str]:
    """Return ``(tenant, wd, site)`` for a Workday board identifier."""
    text = str(board or "").strip()
    if not text:
        raise ValueError(f"Empty Workday board. {_BOARD_HELP}")

    if "://" in text:
        try:
            parsed = urlparse(text)
        except ValueError as exc:
            raise ValueError(f"Invalid Workday board URL: {board}") from exc
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Workday board URL must use http(s): {board}")
        host = (parsed.hostname or "").lower()
        match = _WORKDAY_HOST.match(host)
        if not match:
            raise ValueError(f"Workday board URL must be <tenant>.<wdN>.myworkdayjobs.com: {board}")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2:
            site = parts[-1]
        elif len(parts) == 1:
            site = parts[0]
        else:
            raise ValueError(f"Workday board URL must contain exactly one site path segment: {board}")
        _validate_site(site, board)
        return match.group("tenant").lower(), match.group("wd").lower(), site

    parts = [part.strip() for part in text.split("|")]
    if len(parts) == 2:
        tenant, site = parts
        wd = DEFAULT_WD
    elif len(parts) == 3:
        tenant, wd, site = parts
    else:
        raise ValueError(f"Invalid Workday board '{board}'. {_BOARD_HELP}")

    if not tenant or not site:
        raise ValueError(f"Workday board must include a tenant and a site: {board}")
    if not _TENANT.fullmatch(tenant):
        raise ValueError(f"Invalid Workday tenant '{tenant}': use letters, digits, and hyphens")
    if not _WD.fullmatch(wd):
        raise ValueError(f"Invalid Workday data center '{wd}': expected wd1, wd2, etc.")
    _validate_site(site, board)
    return tenant.lower(), wd.lower(), site


def _validate_site(site: str, board: str) -> None:
    if not site or "/" in site or "\\" in site or any(ch.isspace() for ch in site):
        raise ValueError(f"Invalid Workday site '{site}' in board '{board}'")


def parse_posted_on(value: Any) -> str | None:
    """Normalize an absolute Workday ``postedOn`` value.

    The CXS list endpoint commonly returns relative labels such as
    "Posted 3 Days Ago". Those are not dates and are left as ``None``.
    """
    if value is None:
        return None
    text = re.sub(r"^posted(?:\s+on)?\s*:?\s*", "", str(value).strip(), flags=re.IGNORECASE)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.isoformat(timespec="seconds")
    except ValueError:
        pass
    for fmt in _POSTED_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
            return parsed.isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    last_error: Exception | None = None
    for attempt in range(RETRIES + 1):
        _LIMITER.wait(url)
        try:
            response = requests.post(
                url, json=payload, timeout=TIMEOUT, headers=headers, stream=True
            )
            try:
                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After", "2")
                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        try:
                            retry_at = parsedate_to_datetime(retry_after)
                            if retry_at.tzinfo is None:
                                retry_at = retry_at.replace(tzinfo=timezone.utc)
                            delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                        except (TypeError, ValueError):
                            delay = 2.0
                    last_error = SourceError(f"HTTP 429 for {url}")
                    if attempt < RETRIES:
                        time.sleep(min(max(delay, 0.0), 10.0))
                    continue
                if response.status_code >= 500:
                    last_error = SourceError(f"HTTP {response.status_code} for {url}")
                elif response.status_code >= 400:
                    raise SourceError(f"HTTP {response.status_code} for {url}")
                else:
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_content(chunk_size=32768):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise SourceError(f"Workday response exceeds {MAX_BYTES} bytes: {url}")
                        chunks.append(chunk)
                    try:
                        data = json.loads(b"".join(chunks))
                    except ValueError as exc:
                        raise SourceError(f"Invalid JSON from {url}: {exc}") from exc
                    if not isinstance(data, dict):
                        raise SourceError(f"Unexpected Workday response from {url}")
                    return data
            finally:
                response.close()
        except requests.RequestException as exc:
            last_error = exc
        if attempt < RETRIES:
            time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"Failed to fetch {url}: {last_error}")


def _detail_description(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    info = data.get("jobPostingInfo")
    candidates = [
        info.get("jobDescription") if isinstance(info, dict) else None,
        info.get("externalDescription") if isinstance(info, dict) else None,
        data.get("jobDescription"),
        data.get("description"),
    ]
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return html_to_text(candidate)
    return ""


class WorkdaySource(JobSource):
    name = "workday"

    def fetch(
        self,
        board: str,
        *,
        max_jobs: int = 100,
        page_size: int = 20,
        include_details: bool = True,
        **kwargs: Any,
    ) -> list[Job]:
        tenant, wd, site = parse_board(board)
        list_url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
        company = company_from_board(tenant)
        jobs: list[Job] = []
        seen: set[str] = set()
        offset = 0

        while len(jobs) < max_jobs:
            data = _post_json(
                list_url,
                {
                    "appliedFacets": {},
                    "limit": page_size,
                    "offset": offset,
                    "searchText": "",
                },
            )
            postings = data.get("jobPostings") or []
            if not isinstance(postings, list):
                raise SourceError(f"Workday response for {board} did not contain a jobPostings list")
            if not postings:
                break

            new_jobs = 0
            for item in postings:
                if len(jobs) >= max_jobs:
                    break
                if not isinstance(item, dict):
                    continue
                external_path = str(item.get("externalPath") or "").strip()
                if not external_path or external_path in seen:
                    continue
                seen.add(external_path)
                path = external_path if external_path.startswith("/") else f"/{external_path}"
                apply_url = f"https://{tenant}.{wd}.myworkdayjobs.com/en-US/{site}{path}"
                location = str(item.get("locationsText") or "")
                jobs.append(
                    Job(
                        source=self.name,
                        source_id=external_path,
                        company=company,
                        title=str(item.get("title") or "Untitled"),
                        location=location,
                        remote=is_remote(location),
                        url=apply_url,
                        apply_url=apply_url,
                        posted_at=parse_posted_on(item.get("postedOn")),
                        raw=item,
                    )
                )
                new_jobs += 1

            # A page of only duplicates (or a tenant that ignores offset) must
            # not spin forever when the response omits a total.
            if new_jobs == 0:
                break

            offset += len(postings)
            total = data.get("total")
            if len(postings) < page_size:
                break
            if isinstance(total, int) and offset >= total:
                break

        if include_details:
            for job in jobs:
                job.description = self._fetch_description(tenant, wd, site, job.source_id)
        return jobs

    @staticmethod
    def _fetch_description(tenant: str, wd: str, site: str, external_path: str) -> str:
        path = external_path if external_path.startswith("/") else f"/{external_path}"
        url = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{path}"
        try:
            data = http_get_json(url, headers={"Accept": "application/json"})
        except (SourceError, ValueError, TypeError):
            return ""
        return _detail_description(data)
