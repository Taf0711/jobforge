"""Role-archetype detection and project ranking.

The importer can copy a role-archetypes YAML file (resume-forge format) into the
local app home. When present, tailoring uses the detected archetype's
``strongest_projects`` list as a priority signal, then fills remaining slots from
the JD skill/domain score. This preserves a curated project slate instead of
letting generic keyword overlap pick the resume projects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .jd import JD

# Extra signals that are common in JDs but not always present in a hand-written
# archetype file. They only add weight; the file's jd_signals remain primary.
EXTRA_SIGNALS: dict[str, list[str]] = {
    "systems_networking_embedded": [
        "c++", "rust", "distributed systems", "operating systems", "query engine",
        "query engines", "database", "storage", "compiler", "low latency",
        "performance", "hpc", "simd", "kernel",
    ],
    "backend_platform": [
        "backend", "platform", "api", "microservices", "distributed systems",
        "databases", "python", "java", "scala", "spark",
    ],
    "ai_llm": [
        "ai", "llm", "genai", "rag", "nlp", "agents", "gemini", "openai",
        "pytorch", "inference", "model evaluation",
    ],
    "ml_computer_vision": [
        "machine learning", "computer vision", "opencv", "yolo", "pytorch",
        "cuda", "model training", "data pipeline",
    ],
    "security_identity_compliance": [
        "security", "identity", "access control", "rbac", "jwt", "audit",
        "compliance", "policy", "authentication", "authorization",
    ],
    "cloud_devops_automation": [
        "aws", "lambda", "terraform", "ci/cd", "jenkins", "docker",
        "kubernetes", "automation", "infrastructure", "platform",
    ],
    "fullstack_product": [
        "full stack", "frontend", "backend", "react", "typescript",
        "javascript", "rest api", "sql", "product", "web",
    ],
    "frontend_web": [
        "frontend", "react", "typescript", "javascript", "html", "css",
        "responsive", "ui", "design system",
    ],
    "government_defense_regulated": [
        "us citizen", "clearance", "itar", "government", "defense",
        "compliance", "audit", "secure systems", "public sector",
    ],
    "it_support_infrastructure": [
        "it support", "desktop support", "windows", "macos", "hardware",
        "printers", "network troubleshooting", "tickets", "help desk",
    ],
}


@dataclass
class ArchetypeMatch:
    name: str
    label: str
    score: int
    spec: dict[str, Any]


def load_archetypes(path: str | Path) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8")) or {}
    archetypes = data.get("archetypes", data) if isinstance(data, dict) else {}
    if not isinstance(archetypes, dict):
        return {}
    return {
        str(name): dict(spec)
        for name, spec in archetypes.items()
        if isinstance(spec, dict)
    }


def _contains(text: str, signal: str) -> bool:
    signal = signal.strip().lower()
    if not signal:
        return False
    if len(signal) <= 3:
        return re.search(rf"(?<![a-z0-9]){re.escape(signal)}(?![a-z0-9])", text) is not None
    return signal in text


def detect_archetype(
    jd: JD, archetypes: dict[str, dict[str, Any]]
) -> ArchetypeMatch | None:
    text = f"{jd.title}\n{jd.description}".lower()
    title = (jd.title or "").lower()
    best: ArchetypeMatch | None = None
    for name, spec in archetypes.items():
        signals = [str(item) for item in spec.get("jd_signals", []) or []]
        signals.extend(EXTRA_SIGNALS.get(name, []))
        score = 0
        for signal in signals:
            if not _contains(text, signal):
                continue
            score += 2 if _contains(title, signal) else 1
        candidate = ArchetypeMatch(
            name=name,
            label=str(spec.get("label", name)),
            score=score,
            spec=spec,
        )
        if best is None or candidate.score > best.score:
            best = candidate
    return best if best and best.score > 0 else None
