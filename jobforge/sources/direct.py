"""Fetch and normalize a single public job URL (paste-a-link flow)."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..models import Job
from .base import (
    assert_public_url,
    html_to_text,
    http_get,
    is_remote,
    iso_from_string,
    parse_salary,
)

ATS_HOSTS = {
    "greenhouse.io": "greenhouse",
    "job-boards.greenhouse.io": "greenhouse",
    "lever.co": "lever",
    "jobs.lever.co": "lever",
    "ashbyhq.com": "ashby",
    "jobs.ashbyhq.com": "ashby",
    "workable.com": "workable",
    "apply.workable.com": "workable",
    "smartrecruiters.com": "smartrecruiters",
    "jobs.smartrecruiters.com": "smartrecruiters",
    "myworkdayjobs.com": "workday",
    "myworkdaysite.com": "workday",
    "indeed.com": "indeed",
}


def detect_ats(url: str, html: str = "") -> str:
    host = (urlparse(url).hostname or "").lower()
    for suffix, ats in ATS_HOSTS.items():
        if host == suffix or host.endswith("." + suffix):
            return ats
    haystack = html.lower()
    if "greenhouse.io" in haystack:
        return "greenhouse"
    if "lever.co" in haystack:
        return "lever"
    if "ashbyhq.com" in haystack:
        return "ashby"
    if "workable.com" in haystack:
        return "workable"
    if "myworkdayjobs.com" in haystack:
        return "workday"
    return "generic"


def _json_ld(soup: BeautifulSoup) -> dict[str, Any] | None:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except (TypeError, ValueError):
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _location_from_json_ld(item: dict[str, Any]) -> str:
    locations = item.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    parts: list[str] = []
    for loc in locations:
        address = (loc or {}).get("address") or {}
        chunk = ", ".join(
            filter(None, [address.get("addressLocality"), address.get("addressRegion"), address.get("addressCountry")])
        )
        if chunk:
            parts.append(chunk)
    if not parts and item.get("jobLocationType") == "TELECOMMUTE":
        return "Remote"
    return "; ".join(parts)


def _salary_from_json_ld(item: dict[str, Any]) -> tuple[int | None, int | None]:
    salary = item.get("baseSalary") or {}
    value = salary.get("value") if isinstance(salary, dict) else None
    if isinstance(value, dict):
        lo = value.get("minValue") or value.get("value")
        hi = value.get("maxValue") or value.get("value")
        try:
            return (int(lo) if lo else None, int(hi) if hi else None)
        except (TypeError, ValueError):
            pass
    return parse_salary(json.dumps(salary))


GENERIC_COMPANIES = {
    "job boards",
    "job application",
    "greenhouse",
    "lever",
    "ashby",
    "workday",
    "workable",
    "smartrecruiters",
    "careers",
    "jobs",
    "globalhr",
    "apply",
    "untitled",
}
GENERIC_HOST_LABELS = {"www", "jobs", "job", "job-boards", "boards", "careers", "apply"}
WORKDAY_GENERIC_TOKENS = {
    "rec", "ext", "external", "internal", "gateway", "careers", "career",
    "jobs", "job", "site", "globalhr", "wd1", "wd3", "wd5", "wd12",
    "en-us", "apply", "application",
}
LOCATION_SELECTORS = (
    "[data-automation-id='jobPostingLocation']",
    "[data-automation-id*='location']",
    "[data-testid*='location']",
    "[itemprop='jobLocation']",
    "[class*='job-location']",
    "[class*='location']",
    "#location",
    ".location",
)
DESCRIPTION_SPECIFIC_SELECTORS = (
    "[data-automation-id='jobPostingDescription']",
    "[data-automation-id*='jobPostingDescription']",
    "[data-testid*='job-description']",
    "[class*='job-description']",
    "[class*='jobDescription']",
    "[class*='description']",
    "[itemprop='description']",
)
DESCRIPTION_GENERIC_SELECTORS = (
    "main",
    "article",
    "[role='main']",
    "#content",
    "#app_body",
)
DROP_DESCRIPTION_LINES = {
    "back to jobs",
    "apply",
    "apply now",
    "apply for this job",
    "submit application",
    "skip to main content",
    "view job",
    "share",
    "save",
    "careers",
    "open roles",
    "/",
}


def _meta(soup: BeautifulSoup, *names: str) -> str:
    for name in names:
        for attr in ("property", "name", "itemprop"):
            tag = soup.find("meta", attrs={attr: name})
            if tag:
                value = str(tag.get("content") or "").strip()
                if value:
                    return value
    return ""


def _clean_company(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"\s+(?:logo|careers|jobs|career site)$", "", text, flags=re.IGNORECASE)
    text = text.strip(" -|·•")
    if not text or text.lower() in GENERIC_COMPANIES:
        return ""
    return text[:120]


def _company_from_title(title: str) -> str:
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    patterns = (
        r"^job application for .+? at (.+)$",
        r"^.+?@\s*(.+)$",
        r"^.+?\|\s*(?:careers|jobs)\s*\|\s*(.+)$",
        r"^.+?\bat\s+(.+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def _company_from_logo(soup: BeautifulSoup) -> str:
    for image in soup.find_all("img", alt=True)[:40]:
        alt = str(image.get("alt") or "")
        if "logo" not in alt.lower():
            continue
        cleaned = _clean_company(re.sub(r"\blogo\b", "", alt, flags=re.IGNORECASE))
        if cleaned:
            return cleaned
    return ""


def _title_slug(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if any(character.isupper() for character in text):
        return text
    return text.replace("-", " ").replace("_", " ").title()


def _company_from_url(url: str, ats: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [part for part in parsed.path.split("/") if part]
    if ats == "workday":
        for part in parts:
            for token in re.split(r"[_\-.]+", part.lower()):
                if token in WORKDAY_GENERIC_TOKENS or len(token) < 2 or not token.isalpha():
                    continue
                return token.upper() if token.isupper() or len(token) <= 3 else token.title()
        tenant = host.split(".")[0]
        if tenant and tenant not in GENERIC_HOST_LABELS:
            return _title_slug(tenant)
    if ats in {"greenhouse", "lever", "ashby", "workable", "smartrecruiters"}:
        for part in parts:
            low = part.lower()
            if low in {"jobs", "job", "en-us", "apply", "application", "careers", "career"}:
                continue
            if re.fullmatch(r"[0-9a-f-]{16,}", low):
                continue
            return _title_slug(part)
    label = host.split(".")[0]
    if label in GENERIC_HOST_LABELS:
        pieces = host.split(".")
        label = pieces[-2] if len(pieces) >= 2 else label
    return _title_slug(label)


def _extract_company(
    soup: BeautifulSoup, url: str, raw_title: str, ats: str, ld_company: str = ""
) -> str:
    candidates = (
        ld_company,
        _meta(soup, "og:site_name", "application-name", "apple-mobile-web-app-title"),
        _company_from_title(raw_title),
        _company_from_logo(soup),
        _company_from_url(url, ats),
    )
    for value in candidates:
        cleaned = _clean_company(value)
        if cleaned:
            return cleaned
    return ""


def _clean_title(value: str, company: str = "") -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"^job application for\s+", "", text, flags=re.IGNORECASE)
    if company:
        text = re.sub(rf"\s+at\s+{re.escape(company)}\s*$", "", text, flags=re.IGNORECASE)
        text = re.sub(
            rf"^{re.escape(company)}\s*[-|@]\s*", "", text, flags=re.IGNORECASE
        )
    text = re.sub(r"\s+\|\s+careers\s+\|\s+.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+@\s+.*$", "", text)
    text = re.sub(r"\s+\|\s+.*$", "", text)
    text = re.sub(r"\s+", " ", text).strip(" -|")
    return text[:300]


def _extract_title(
    soup: BeautifulSoup, ld: dict[str, Any] | None, company: str = ""
) -> str:
    h1 = soup.find("h1")
    title_tag = soup.title.get_text(" ", strip=True) if soup.title else ""
    candidates = (
        str((ld or {}).get("title") or ""),
        _meta(soup, "og:title", "twitter:title"),
        h1.get_text(" ", strip=True) if h1 else "",
        title_tag,
    )
    for value in candidates:
        cleaned = _clean_title(value, company)
        if cleaned and cleaned.lower() not in {"untitled", "careers", "jobs"}:
            return cleaned
    return "Untitled"


def _looks_like_location(value: str) -> bool:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text or len(text) > 200 or "\n" in str(value):
        return False
    if text.endswith((".", "!", "?")):
        return False
    if len(text.split()) > 14:
        return False
    if re.search(
        r"\b(apply|about|role|team|we are|responsibilities|qualifications|job description|salary|compensation|benefits)\b",
        text,
        re.IGNORECASE,
    ):
        return False
    if re.search(r"\b(remote|hybrid|onsite|on-site|anywhere|distributed)\b", text, re.IGNORECASE):
        return True
    return "," in text


def _location_from_text(value: str) -> str:
    match = re.search(
        r"\bLocation\s*:\s*(.+?)(?:\s+(?:Position|Job|Time|Type|Role|Date)\b|$)",
        str(value or ""),
        re.IGNORECASE,
    )
    if not match:
        return ""
    text = re.sub(r"\s+", " ", match.group(1)).strip(" .,;")
    return text[:200] if text else ""


def _extract_location(
    soup: BeautifulSoup, ld: dict[str, Any] | None, url: str
) -> str:
    ld_location = _location_from_json_ld(ld or {})
    if ld_location:
        return ld_location
    metas = (
        _meta(soup, "og:description"),
        _meta(soup, "twitter:description"),
        _meta(soup, "description"),
    )
    for value in metas:
        if _looks_like_location(value):
            return re.sub(r"\s+", " ", value).strip()[:200]
    for selector in LOCATION_SELECTORS:
        for element in soup.select(selector):
            text = re.sub(r"\s+", " ", element.get_text(" ", strip=True)).strip()
            text = re.sub(r"^(?:job\s+)?location\s*:\s*", "", text, flags=re.IGNORECASE)
            if _looks_like_location(text):
                return text[:200]
    for value in metas:
        match = re.search(r"[—–-]\s*(.+?)\s+at\s+[^.]+\.?$", value)
        if match and _looks_like_location(match.group(1)):
            return match.group(1).strip()[:200]
        labeled = _location_from_text(value)
        if labeled:
            return labeled
    labeled = _location_from_text(soup.get_text("\n"))
    return labeled


def _best_text(soup: BeautifulSoup, selectors: tuple[str, ...]) -> str:
    best = ""
    for selector in selectors:
        for element in soup.select(selector):
            text = html_to_text(element.decode_contents())
            if len(text) > len(best):
                best = text
    return best


def _extract_description(soup: BeautifulSoup, ld: dict[str, Any] | None) -> str:
    if ld:
        description = html_to_text(ld.get("description") or "")
        if len(description) >= 100:
            return description
    og_description = _meta(soup, "og:description", "twitter:description")
    if len(og_description) >= 500 or re.search(
        r"\b(?:date posted|location|position role type|responsibilities|qualifications)\b",
        og_description,
        re.IGNORECASE,
    ):
        return og_description
    specific = _best_text(soup, DESCRIPTION_SPECIFIC_SELECTORS)
    if len(specific) >= 200:
        return specific
    generic = _best_text(soup, DESCRIPTION_GENERIC_SELECTORS)
    if len(generic) >= 200:
        return generic
    return html_to_text(soup.get_text("\n"))


def _clean_description(
    value: str, title: str = "", company: str = "", location: str = ""
) -> str:
    lines: list[str] = []
    for raw in str(value or "").splitlines():
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        low = line.lower()
        if low in DROP_DESCRIPTION_LINES:
            continue
        if len(lines) < 12 and re.fullmatch(r"[A-Za-z][A-Za-z &/+'-]{2,60}\s+\d{2,}", line):
            continue
        if low.startswith("job application for "):
            continue
        if title and (low == title.lower() or low.startswith(title.lower()) and len(line) <= len(title) + 40):
            continue
        if company and low == company.lower():
            continue
        if location and low == location.lower():
            continue
        if re.fullmatch(r"[|•·\-–—]+", line):
            continue
        lines.append(line)
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def fetch_job_url(url: str) -> Job:
    assert_public_url(url)
    resp = http_get(url, headers={"Accept": "text/html,application/xhtml+xml"})
    html = resp.text
    soup = BeautifulSoup(html, "html.parser")
    ats = detect_ats(url, html)
    ld = _json_ld(soup) or {}
    raw_title = _meta(soup, "og:title", "twitter:title") or (
        soup.title.get_text(" ", strip=True) if soup.title else ""
    )
    ld_company = str((ld.get("hiringOrganization") or {}).get("name") or "")
    company = _extract_company(soup, url, raw_title, ats, ld_company)
    title = _extract_title(soup, ld, company)
    location = _extract_location(soup, ld, url)
    description = _extract_description(soup, ld)
    if ld:
        smin, smax = _salary_from_json_ld(ld)
        posted = iso_from_string(ld.get("datePosted"))
        apply_url = str(ld.get("url") or url)
        source_id = str((ld.get("identifier") or {}).get("value") or "")
    else:
        smin, smax = parse_salary(description)
        posted = None
        apply_url = url
        source_id = ""
    if smin is None and smax is None:
        smin, smax = parse_salary(description)
    description = _clean_description(description, title, company, location)[:60_000]
    if not company:
        company = _company_from_url(url, ats) or "Unknown"
    if not source_id:
        source_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return Job(
        source=ats if ats != "generic" else "direct",
        source_id=source_id,
        company=company,
        title=title,
        location=location,
        remote=is_remote(location, description),
        salary_min=smin,
        salary_max=smax,
        url=url,
        apply_url=apply_url,
        description=description,
        posted_at=posted,
        raw={"ats": ats},
    )
