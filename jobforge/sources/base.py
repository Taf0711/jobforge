"""Shared HTTP, parsing, and normalization for job sources."""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = "JobForge/0.1 (+local job search; contact: local-user)"
DEFAULT_TIMEOUT = 30
MAX_BYTES = 5 * 1024 * 1024


class SourceError(RuntimeError):
    pass


class RateLimiter:
    """Small per-host token bucket; keeps public APIs polite."""

    def __init__(self, min_interval: float = 0.4) -> None:
        self.min_interval = min_interval
        self._last: dict[str, float] = {}

    def wait(self, url: str) -> None:
        host = urlparse(url).netloc
        now = time.monotonic()
        last = self._last.get(host, 0.0)
        delay = self.min_interval - (now - last)
        if delay > 0:
            time.sleep(delay)
        self._last[host] = time.monotonic()


_LIMITER = RateLimiter()


def _retry_after_seconds(value: str | None) -> float:
    """Parse a numeric or HTTP-date Retry-After value; default 2 seconds."""
    if not value:
        return 2.0
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return (retry_at - datetime.now(timezone.utc)).total_seconds()
    except (TypeError, ValueError):
        return 2.0


def http_get(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = 3,
    headers: dict[str, str] | None = None,
    max_bytes: int = MAX_BYTES,
) -> requests.Response:
    """GET with retries/backoff and a hard response-size cap."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SourceError(f"Refusing non-http(s) URL: {url}")
    _LIMITER.wait(url)
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": USER_AGENT, "Accept": "*/*", **(headers or {})},
                stream=True,
            )
            if resp.status_code == 429:
                resp.close()
                retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
                time.sleep(min(max(retry_after, 0.0), 10.0))
                continue
            if resp.status_code >= 400:
                raise SourceError(f"HTTP {resp.status_code} for {url}")
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=32768):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise SourceError(f"Response exceeds {max_bytes} bytes: {url}")
                chunks.append(chunk)
            resp._content = b"".join(chunks)  # type: ignore[attr-defined]
            return resp
        except (requests.RequestException, SourceError) as exc:
            last_error = exc
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise SourceError(f"Failed to fetch {url}: {last_error}")


def http_get_json(url: str, **kwargs: Any) -> Any:
    resp = http_get(url, **kwargs)
    try:
        return resp.json()
    except ValueError as exc:
        raise SourceError(f"Invalid JSON from {url}: {exc}") from exc


def html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    # Join inline-tag breaks (e.g. "Hello\nworld") without destroying paragraphs.
    text = re.sub(r"\n(?=[a-z0-9(])", " ", text)
    return normalize_whitespace(text)


def normalize_whitespace(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def parse_salary(text: str | None) -> tuple[int | None, int | None]:
    """Extract a salary range from free text. Conservative: requires a $ or K."""
    if not text:
        return None, None
    raw = str(text).replace(",", "")
    # $120,000 - $160,000 / $120K-$160K / 120000-160000 USD
    pattern = re.compile(
        r"(\$?\s*\d{3,7}(?:\.\d+)?\s*[kK]?)\s*(?:-|–|—|to)\s*(\$?\s*\d{3,7}(?:\.\d+)?\s*[kK]?)"
    )
    m = pattern.search(raw)
    if not m:
        single = re.search(r"\$\s*\d{3,7}(?:\.\d+)?\s*[kK]?", raw)
        if single:
            value = _to_int(single.group(0))
            return value, value
        return None, None
    left, right = m.group(1), m.group(2)
    if "$" not in left + right and "k" not in (left + right).lower():
        return None, None
    return _to_int(left), _to_int(right)


def _to_int(value: str) -> int | None:
    cleaned = value.replace("$", "").replace(" ", "").strip()
    if not cleaned:
        return None
    multiplier = 1
    if cleaned.lower().endswith("k"):
        multiplier = 1000
        cleaned = cleaned[:-1]
    try:
        number = float(cleaned)
    except ValueError:
        return None
    number *= multiplier
    if number < 1000:
        return None
    return int(round(number))


def is_remote(location: str, description: str = "", flag: bool | None = None) -> bool:
    if flag:
        return True
    haystack = f"{location} {description[:2000]}".lower()
    return any(token in haystack for token in ("remote", "work from home", "distributed"))


def iso_from_epoch_ms(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return None


def iso_from_string(value: Any) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat(timespec="seconds")
    except ValueError:
        return text


def company_from_board(board: str) -> str:
    return board.replace("-", " ").replace("_", " ").title()


class JobSource(ABC):
    name: str = "base"

    @abstractmethod
    def fetch(self, board: str, **kwargs: Any) -> list:
        """Return a list of models.Job for one board/company."""


def assert_public_url(url: str) -> None:
    """Reject SSRF targets: non-http(s), localhost, private/link-local IPs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise SourceError("Only public http(s) URLs are allowed")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise SourceError("Localhost URLs are not allowed")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise SourceError(f"Cannot resolve host: {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise SourceError(f"Refusing private/internal address for {host}")
