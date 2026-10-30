"""Skill normalization, extraction, and partial-credit coverage."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

from .lexicon import ALIASES, SKILLS, STOP_SKILLS

GRAPH_PATH = Path(__file__).with_name("skill_graph.yml")


def canonical(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    key = cleaned.lower()
    if key in ALIASES:
        return ALIASES[key]
    for skill in SKILLS:
        if skill.lower() == key:
            return skill
    return cleaned


def _pattern(skill: str) -> re.Pattern[str]:
    escaped = re.escape(skill)
    if skill == "C":
        return re.compile(r"(?<![\w+])C(?![\w+])")
    if skill == "R":
        return re.compile(r"(?<![\w.])R(?![\w.])")
    if skill == "Go":
        return re.compile(r"(?<![\w])Go(?![\w])")
    if skill in {"AI", "ML"}:
        return re.compile(rf"(?<![\w]){escaped}(?![\w])")
    return re.compile(rf"(?<![\w]){escaped}(?![\w])", re.IGNORECASE)


def extract_skills(text: str) -> list[str]:
    """Return canonical skills mentioned in text, ordered by frequency."""
    if not text:
        return []
    counts: dict[str, int] = {}
    all_names = list(dict.fromkeys([*SKILLS, *ALIASES.keys()]))
    for name in all_names:
        if name.lower() in STOP_SKILLS:
            continue
        if name == "C":
            # "C-level", "C-suite", and the letter C in prose are false positives.
            if not re.search(
                r"C\+\+|C programming|C language|^[^\n]*\bC\b(?!-level|-suite|-executive)[^\n]*(,|/)[^\n]*$",
                text,
                re.MULTILINE,
            ):
                continue
        if name == "R":
            if not re.search(r"R programming|R language|RStudio|\bR\b\s*[,/]", text):
                continue
        found = len(_pattern(name).findall(text))
        if found:
            canon = canonical(name)
            counts[canon] = counts.get(canon, 0) + found
    # Aliases can double-count the canonical name; keep max, not sum.
    return [s for s, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]


@dataclass
class CoverageRow:
    requirement: str
    skill: str | None
    importance: str
    credit: float
    weight: float
    matched_by: str
    gap: bool


@dataclass
class CoverageResult:
    coverage: float
    rows: list[CoverageRow] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @property
    def table(self) -> list[dict[str, Any]]:
        return [
            {
                "requirement": r.requirement,
                "skill": r.skill,
                "importance": r.importance,
                "credit": round(r.credit, 3),
                "weight": r.weight,
                "matched_by": r.matched_by,
                "gap": r.gap,
            }
            for r in self.rows
        ]


class SkillGraph:
    def __init__(self, path: str | Path = GRAPH_PATH) -> None:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        self.families: dict[str, dict[str, Any]] = data.get("families", {}) or {}
        self.edges: dict[str, dict[str, float]] = {}
        for left, right, weight in data.get("edges", []) or []:
            a, b = canonical(left), canonical(right)
            self.edges.setdefault(a, {})[b] = float(weight)
            self.edges.setdefault(b, {})[a] = float(weight)
        self._family_of: dict[str, str] = {}
        for family, spec in self.families.items():
            for member in spec.get("members", []):
                self._family_of[canonical(member)] = family

    def partial_credit(self, wanted: str, owned: set[str]) -> tuple[float, str]:
        wanted = canonical(wanted)
        if wanted in owned:
            return 1.0, "direct"
        family = self._family_of.get(wanted)
        if family:
            spec = self.families[family]
            for member in spec.get("members", []):
                if canonical(member) in owned:
                    return float(spec.get("partial", 0.5)), f"adjacent:{canonical(member)}"
        best = 0.0
        best_match = ""
        for other, weight in self.edges.get(wanted, {}).items():
            if other in owned and weight > best:
                best, best_match = weight, other
        if best:
            return best, f"edge:{best_match}"
        return 0.0, "none"


def coverage(
    requirements: Iterable[dict[str, Any]],
    profile_skills: Iterable[str],
    graph: SkillGraph,
    evidence: dict[str, float] | None = None,
    *,
    prose_credit: float = 0.60,
    evidence_direct: bool = False,
) -> CoverageResult:
    owned = {canonical(s) for s in profile_skills if s}
    # Evidence may use an alias (for example "cpp" for "C++"). Normalize keys
    # so direct and transfer-credit discounts agree on the source skill.
    evidence = {canonical(key): value for key, value in (evidence or {}).items() if key}
    rows: list[CoverageRow] = []
    total_weight = 0.0
    total_credit = 0.0
    gaps: list[str] = []
    seen: set[tuple[str, str]] = set()

    for req in requirements:
        text = str(req.get("text") or "").strip()
        skill = req.get("skill")
        skill = canonical(skill) if skill else None
        importance = str(req.get("importance") or "must").lower()
        if importance == "context":
            continue
        if skill:
            key = (skill, importance)
            if key in seen:
                continue
            seen.add(key)
        weight = 1.0 if importance == "must" else 0.4
        if not skill:
            # Unparseable prose is reported in the table but cannot be verified
            # against the profile. New scoring gives it no positive credit; the
            # legacy path keeps the old neutral-credit calibration.
            credit, matched_by = max(0.0, min(1.0, prose_credit)), "unparsed"
        else:
            credit, matched_by = graph.partial_credit(skill, owned)
            # Direct credit is discounted only in evidence-aware scoring;
            # transfer credit is always discounted by the evidence behind the
            # skill that earned it. Otherwise an adjacent skill with weak
            # evidence can outrank the directly named skill.
            evidence_skill = skill
            if matched_by.startswith(("adjacent:", "edge:")):
                evidence_skill = matched_by.split(":", 1)[1]
            if credit > 0 and (matched_by != "direct" or evidence_direct):
                source_evidence = evidence.get(evidence_skill)
                if source_evidence is not None:
                    credit *= max(0.0, min(1.0, source_evidence))
            if credit == 0 and skill in owned:
                credit = 1.0
        total_weight += weight
        total_credit += weight * credit
        gap = credit < 0.5
        if gap and skill:
            gaps.append(skill)
        rows.append(
            CoverageRow(
                requirement=text or (skill or ""),
                skill=skill,
                importance=importance,
                credit=credit,
                weight=weight,
                matched_by=matched_by,
                gap=gap,
            )
        )

    score = (total_credit / total_weight) if total_weight else 0.0
    # Deduplicate gaps while preserving order.
    gaps = list(dict.fromkeys(gaps))
    return CoverageResult(coverage=max(0.0, min(1.0, score)), rows=rows, gaps=gaps)
