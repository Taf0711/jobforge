"""Curated source presets for the first-run setup wizard.

The presets are a starting point, not a claim that every board is still live;
the normal scan/verify workflow still applies.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

ATS_KEYS = ("greenhouse", "lever", "ashby", "workable", "workday", "smartrecruiters")
PUBLIC_KEYS = ("nyc_opendata", "usajobs")

PRESETS: dict[str, dict[str, Any]] = {
    "ai": {
        "label": "AI & developer tools",
        "description": "AI labs and AI-native startups on Ashby, plus Anthropic, Scale AI, and Databricks.",
        "sources": {
            "greenhouse": ["anthropic", "scaleai", "databricks"],
            "ashby": [
                "baseten",
                "perplexity",
                "cohere",
                "fireworks",
                "modal",
                "decagon",
                "sierra",
                "elevenlabs",
                "browserbase",
                "cognition",
            ],
        },
    },
    "fintech": {
        "label": "Fintech & banking",
        "description": "Fintech product teams plus Citi and Capital One Workday boards.",
        "sources": {
            "greenhouse": [
                "stripe",
                "block",
                "robinhood",
                "brex",
                "mercury",
                "chime",
                "affirm",
                "adyen",
                "coinbase",
                "sofi",
                "toast",
            ],
            "workday": ["citi|wd5|2", "capitalone|wd12|Capital_One"],
        },
    },
    "product": {
        "label": "Product & infrastructure",
        "description": "Discord, Cloudflare, Datadog, Linear, MetLife, and Seedlegals.",
        "sources": {
            "greenhouse": ["discord", "cloudflare", "datadog"],
            "ashby": ["linear"],
            "lever": ["metlife"],
            "workable": ["seedlegals"],
        },
    },
    "public": {
        "label": "Public sector",
        "description": "NYC Open Data; USAJobs when its free API key is configured.",
        "sources": {
            "nyc_opendata": {"query": "software", "limit": 200, "enabled": True},
            "usajobs": {"keyword": "software engineer", "limit": 50, "enabled": True},
        },
    },
}


def configured_boards(cfg: Any, name: str) -> list[str]:
    section = (cfg.sources or {}).get(name) or {}
    if not isinstance(section, dict):
        return []
    values = section.get("boards") or section.get("companies") or []
    return [str(value) for value in values if str(value).strip()]


def _public_enabled(cfg: Any) -> bool:
    for name in PUBLIC_KEYS:
        section = (cfg.sources or {}).get(name)
        if isinstance(section, dict) and section.get("enabled"):
            return True
    return False


def _preset_sources() -> dict[str, dict[str, Any]]:
    return {key: spec["sources"] for key, spec in PRESETS.items() if isinstance(spec.get("sources"), dict)}


def selected_presets(cfg: Any) -> list[str]:
    selected: list[str] = []
    for key, sources in _preset_sources().items():
        if any(source in sources for source in PUBLIC_KEYS):
            if _public_enabled(cfg):
                selected.append(key)
            continue
        wanted = {
            f"{source}:{value}"
            for source in ATS_KEYS
            for value in sources.get(source, []) or []
        }
        configured = {
            f"{source}:{value}"
            for source in ATS_KEYS
            for value in configured_boards(cfg, source)
        }
        if wanted and wanted <= configured:
            selected.append(key)
    return selected


def _preset_values(keys: Iterable[str] | None = None) -> dict[str, set[str]]:
    wanted = set(_preset_sources()) if keys is None else set(keys)
    values: dict[str, set[str]] = {source: set() for source in ATS_KEYS}
    for key, sources in _preset_sources().items():
        if key not in wanted:
            continue
        for source in ATS_KEYS:
            values[source].update(str(value) for value in sources.get(source, []) or [])
    return values


def custom_boards(cfg: Any) -> dict[str, str]:
    preset_values = _preset_values(selected_presets(cfg))
    result: dict[str, str] = {}
    for source in ATS_KEYS:
        custom = [
            value
            for value in configured_boards(cfg, source)
            if value not in preset_values[source]
        ]
        if custom:
            result[source] = ", ".join(dict.fromkeys(custom))
    return result


def _split_values(value: str) -> list[str]:
    # Workday boards can contain "|" and URLs; commas/newlines are the only
    # separators here.
    return [part.strip() for part in re.split(r"[\n,]", value or "") if part.strip()]


def build_sources(
    selected: Iterable[str],
    custom: dict[str, str],
) -> dict[str, Any]:
    merged: dict[str, list[str]] = {source: [] for source in ATS_KEYS}
    public: dict[str, dict[str, Any]] = {}
    for key in selected:
        sources = _preset_sources().get(key) or {}
        for source in ATS_KEYS:
            for value in sources.get(source, []) or []:
                merged[source].append(str(value))
        for source in PUBLIC_KEYS:
            if isinstance(sources.get(source), dict):
                public[source] = dict(sources[source])

    for source in ATS_KEYS:
        for value in _split_values(custom.get(source, "")):
            merged[source].append(value)

    result: dict[str, Any] = {}
    for source in ATS_KEYS:
        values = list(dict.fromkeys(merged[source]))
        if not values:
            continue
        key = "companies" if source == "smartrecruiters" else "boards"
        result[source] = {key: values}
    result.update(public)
    return result


def source_board_count(sources: dict[str, Any]) -> int:
    count = 0
    for source in ATS_KEYS:
        section = sources.get(source)
        if not isinstance(section, dict):
            continue
        values = section.get("boards") or section.get("companies") or []
        count += len([value for value in values if str(value).strip()])
    for source in PUBLIC_KEYS:
        section = sources.get(source)
        if isinstance(section, dict) and section.get("enabled"):
            count += 1
    return count


def public_source_enabled(cfg_or_sources: Any) -> bool:
    sources = getattr(cfg_or_sources, "sources", cfg_or_sources) or {}
    return any(
        isinstance(sources.get(source), dict) and bool(sources[source].get("enabled"))
        for source in PUBLIC_KEYS
    )
