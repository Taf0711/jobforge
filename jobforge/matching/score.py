"""Explainable fit scoring: hard gates × weighted skill coverage."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..models import Job, MatchResult, Preferences, Profile
from .economics import comp_policy, is_nyc_role, location_economics
from .gates import citizenship_survival, clearance_survival, gpa_survival, itar_survival
from .jd import JD, parse_jd
from .skills import SkillGraph, coverage

GRAPH = SkillGraph()

DEGREE_RANK = {"none": 0, "associates": 1, "bachelors": 2, "masters": 3, "phd": 4}
CLEARANCE_RANK = {
    "none": 0,
    "security clearance": 1,
    "public trust": 1,
    "secret": 2,
    "ts/sci": 3,
    "top secret": 3,
}
SENIOR_TITLE = re.compile(r"\b(senior|staff|principal|lead|architect|manager|director|vp|head)\b", re.IGNORECASE)
INTERN_TITLE = re.compile(r"\b(intern|internship|co-op|coop)\b", re.IGNORECASE)


def _profile_degree(profile: Profile) -> str:
    best = "none"
    for edu in profile.education:
        value = str(edu.get("degree", "")).lower()
        if "ph" in value or "doctor" in value:
            rank = "phd"
        elif "master" in value or value in {"m.s", "ms", "m.a", "ma"}:
            rank = "masters"
        elif "bachelor" in value or value in {"b.s", "bs", "b.a", "ba"}:
            rank = "bachelors"
        elif "associate" in value:
            rank = "associates"
        else:
            continue
        if DEGREE_RANK[rank] > DEGREE_RANK[best]:
            best = rank
    return best


def _profile_clearance(profile: Profile, prefs: Preferences) -> str:
    raw = (
        profile.work_auth.get("clearance")
        or profile.disclosures.get("clearance")
        or prefs.security_clearance
        or "none"
    )
    value = str(raw).lower()
    for key in ("ts/sci", "top secret", "secret", "public trust"):
        if key in value:
            return key
    return "none"


def _years(profile: Profile, prefs: Preferences) -> float:
    raw = profile.work_auth.get("years_experience", prefs.years_of_experience)
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _location_gate(job: Job, jd: JD, prefs: Preferences) -> tuple[float, str]:
    if job.remote or jd.remote:
        return 1.0, "Remote role"
    if not prefs.locations:
        return 1.0, "No location preference"
    job_loc = (job.location or jd.location or "").lower()
    if not job_loc:
        return 0.8, "Location not listed"
    for wanted in prefs.locations:
        wanted = wanted.lower().strip()
        if not wanted:
            continue
        if wanted in job_loc or job_loc in wanted:
            return 1.0, f"Location match: {job.location}"
    return 0.55, f"Location mismatch: {job.location}"


TITLE_GENERIC = {
    "engineer", "engineering", "software", "developer", "development", "senior", "junior",
    "staff", "principal", "lead", "manager", "director", "intern", "internship", "analyst",
    "associate", "specialist", "remote", "platform", "product", "data", "backend", "frontend",
    "fullstack", "full-stack", "i", "ii", "iii", "and", "the", "of", "at", "for",
}
HEAD_NOUNS = {"engineer", "developer", "analyst", "scientist", "designer", "manager", "architect"}


def _title_gate(job: Job, prefs: Preferences) -> tuple[float, str]:
    title = job.title.lower()
    wants_intern = any("intern" in t.lower() for t in prefs.experience_levels)
    if INTERN_TITLE.search(title) and not wants_intern:
        return 0.55, "Internship title outside your target levels"
    levels = {level.lower() for level in prefs.experience_levels}
    if SENIOR_TITLE.search(title) and ({"entry", "junior", "internship"} & levels):
        return 0.60, "Senior title vs. entry/junior target"
    if not prefs.titles:
        return 1.0, ""
    for wanted in prefs.titles:
        if wanted.lower().strip() in title:
            return 1.0, f"Title alignment: {job.title}"
    title_tokens = {t.lower() for t in re.findall(r"[a-z0-9+#.]+", title) if len(t) > 2}
    for wanted in prefs.titles:
        wanted_tokens = {
            t.lower()
            for t in re.findall(r"[a-z0-9+#.]+", wanted)
            if len(t) > 2 and t.lower() not in TITLE_GENERIC
        }
        if wanted_tokens and wanted_tokens & title_tokens:
            return 1.0, f"Title alignment: {job.title}"
    if title_tokens & HEAD_NOUNS:
        return 0.85, f"Title is adjacent to your targets: {job.title}"
    return 0.70, f"Title is outside your target families: {job.title}"


def _evidence(profile: Profile) -> dict[str, float]:
    out: dict[str, float] = {}
    for skill in profile.skills:
        out[skill] = max(out.get(skill, 0.0), 0.40)  # skills-list-only evidence
    for fact in profile.facts:
        section = str(fact.get("section", "")).lower()
        weight = 1.0 if section == "experience" else 0.80
        for skill in fact.get("skills", []) or []:
            out[skill] = max(out.get(skill, 0.0), weight)
    return out


def _all_profile_skills(profile: Profile) -> list[str]:
    skills = list(profile.skills)
    for fact in profile.facts:
        skills.extend(fact.get("skills", []) or [])
    return list(dict.fromkeys(skills))


def score_job(
    job: Job,
    profile: Profile,
    prefs: Preferences,
    graph: SkillGraph | None = None,
) -> MatchResult:
    graph = graph or GRAPH
    text = job.description or f"{job.title}\n{job.location}"
    jd = parse_jd(text, title_hint=job.title, company_hint=job.company, location_hint=job.location)

    requirements = jd.requirements
    if not requirements:
        requirements = [{"text": job.title, "skill": s, "importance": "must", "kind": "skill"} for s in jd.skills[:8]]
    if not requirements:
        requirements = [{"text": job.title, "skill": None, "importance": "must", "kind": "prose"}]

    coverage_result = coverage(requirements, _all_profile_skills(profile), graph, _evidence(profile))

    gates: list[tuple[float, str, str]] = []  # (survival, reason, gap)
    years_have = _years(profile, prefs)
    if jd.years_required is not None:
        delta = jd.years_required - years_have
        if delta >= 4:
            gates.append((0.0, "", f"Requires {jd.years_required:g}+ years; profile has {years_have:g}"))
        elif delta >= 2:
            gates.append((0.20, "", f"Requires {jd.years_required:g}+ years; profile has {years_have:g}"))
        elif delta > 0:
            gates.append((0.65, f"Close on experience ({jd.years_required:g}+ required)", ""))
        else:
            gates.append((1.0, f"Experience clears {jd.years_required:g}+ year floor", ""))

    have_degree = _profile_degree(profile)
    if jd.degree:
        if DEGREE_RANK.get(have_degree, 0) < DEGREE_RANK.get(jd.degree, 0):
            gates.append((0.25, "", f"Degree floor: {jd.degree}"))
        else:
            gates.append((1.0, f"Degree clears {jd.degree} floor", ""))

    have_clearance = _profile_clearance(profile, prefs)
    if jd.clearance:
        if CLEARANCE_RANK.get(have_clearance, 0) >= CLEARANCE_RANK.get(jd.clearance, 0):
            gates.append((1.0, f"Clearance matches: {jd.clearance}", ""))
        else:
            eligible = bool(profile.work_auth.get("clearance_eligible", False))
            survival = clearance_survival(active_required=True, eligible=eligible)
            reason = "Clearance-eligible for a clearance requirement" if eligible else ""
            gates.append(
                (
                    survival,
                    reason,
                    f"Clearance required: {jd.clearance}"
                    + ("" if eligible else " (not clearance-eligible)"),
                )
            )

    if jd.citizenship_required:
        survival = citizenship_survival(True, bool(profile.work_auth.get("us_citizen", False)))
        gates.append(
            (
                survival,
                "US citizen — satisfies citizenship requirement" if survival >= 1.0 else "",
                "US citizenship required" if survival < 1.0 else "",
            )
        )

    if jd.itar_required:
        survival = itar_survival(True, bool(profile.work_auth.get("itar_eligible", False)))
        gates.append(
            (
                survival,
                "ITAR-eligible" if survival >= 1.0 else "",
                "ITAR eligibility required" if survival < 1.0 else "",
            )
        )

    if jd.gpa_floor is not None:
        raw_gpa = profile.work_auth.get("gpa")
        try:
            candidate_gpa = float(raw_gpa) if raw_gpa is not None else None
        except (TypeError, ValueError):
            candidate_gpa = None
        survival = gpa_survival(jd.gpa_floor, candidate_gpa)
        if survival >= 1.0:
            gates.append((1.0, f"GPA clears the {jd.gpa_floor:.2f} floor", ""))
        elif survival == 0.5:
            gates.append((0.5, "", f"GPA {jd.gpa_floor:.2f} is slightly above the profile GPA"))
        else:
            gates.append((survival, "", f"GPA floor {jd.gpa_floor:.2f} likely screens this out"))

    if jd.sponsorship is False and prefs.needs_sponsorship:
        gates.append((0.0, "", "Employer will not sponsor a visa"))
    elif jd.sponsorship is True:
        gates.append((1.0, "Visa sponsorship available", ""))

    loc_gate, loc_reason = _location_gate(job, jd, prefs)
    gates.append((loc_gate, loc_reason, "" if loc_gate >= 0.8 else f"Location: {job.location}"))

    title_gate, title_reason = _title_gate(job, prefs)
    gates.append((title_gate, title_reason, "" if title_gate >= 0.8 else "Title level mismatch"))

    if prefs.salary_min and job.salary_max and job.salary_max < prefs.salary_min:
        gates.append((0.80, "", f"Posted max ${job.salary_max:,} below your ${prefs.salary_min:,} floor"))
    elif prefs.salary_min and job.salary_min and job.salary_min >= prefs.salary_min:
        gates.append((1.0, f"Salary band starts at ${job.salary_min:,}", ""))

    survival = 1.0
    for value, _, _ in gates:
        survival *= value
    survival = max(0.0, min(1.0, survival))

    parsed_skills = [r for r in jd.requirements if r.get("skill")]
    thin_jd = not parsed_skills
    score = round(100.0 * coverage_result.coverage * survival * (0.5 if thin_jd else 1.0), 1)
    reasons = [reason for _, reason, _ in gates if reason]
    gaps = [gap for _, _, gap in gates if gap] + coverage_result.gaps
    if thin_jd:
        gaps.insert(0, "Job description is too thin to score reliably")
    if coverage_result.coverage >= 0.75:
        reasons.insert(0, f"Strong skill coverage ({coverage_result.coverage:.0%})")
    elif coverage_result.coverage >= 0.5:
        reasons.insert(0, f"Partial skill coverage ({coverage_result.coverage:.0%})")
    else:
        gaps.insert(0, f"Low skill coverage ({coverage_result.coverage:.0%})")

    salary_low = job.salary_min if job.salary_min is not None else jd.salary_min
    salary_high = job.salary_max if job.salary_max is not None else jd.salary_max
    salary_mid = None
    if salary_low is not None and salary_high is not None:
        salary_mid = (float(salary_low) + float(salary_high)) / 2
    elif salary_low is not None or salary_high is not None:
        salary_mid = float(salary_low if salary_low is not None else salary_high)
    location_text = job.location or jd.location or job.description[:500]
    nyc = is_nyc_role(location_text)
    comp = comp_policy(
        nyc,
        float(salary_low) if salary_low is not None else None,
        float(salary_high) if salary_high is not None else None,
        channel="cold_portal",
        callback_pct=0.0,
        policy=prefs.comp_policy,
    )
    location_mod, location_notes = location_economics(
        location_text, salary_mid, loc_gate
    )
    if comp["modifier"] > 0:
        reasons.append(f"Comp policy: {comp['verdict'].replace('_', ' ')} (+{comp['modifier']})")
        reasons.extend(comp.get("notes", [])[:1])
    elif comp["modifier"] < 0:
        gaps.append(f"Comp policy: {comp['verdict'].replace('_', ' ')} ({comp['modifier']})")
        gaps.extend(comp.get("notes", [])[:1])
    if location_mod > 0:
        reasons.extend(location_notes)
    elif location_mod < 0:
        gaps.extend(location_notes)
    pursuit = round(score + comp["modifier"] + location_mod, 1)
    meta = {
        "pursuit": pursuit,
        "comp": comp,
        "location": {"modifier": location_mod, "notes": location_notes},
    }

    return MatchResult(
        job_id=job.id,
        profile_id=profile.id or 0,
        score=score,
        coverage=coverage_result.coverage,
        gate_survival=survival,
        reasons=list(dict.fromkeys(reasons))[:8],
        gaps=list(dict.fromkeys(gaps))[:10],
        requirements=coverage_result.table,
        meta=meta,
    )


def score_all(
    conn: sqlite3.Connection,
    profile: Profile,
    prefs: Preferences,
    limit: int | None = None,
) -> dict[str, Any]:
    from .. import db

    sql = "SELECT * FROM jobs ORDER BY id"
    params: list[Any] = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    scored = 0
    comp_counts: dict[str, int] = {}
    for row in rows:
        job = db._job_from_row(row)
        result = score_job(job, profile, prefs)
        if job.id is not None:
            db.set_match(conn, result)
            scored += 1
            verdict = str(result.meta.get("comp", {}).get("verdict", "unknown"))
            comp_counts[verdict] = comp_counts.get(verdict, 0) + 1
    return {"scored": scored, "total": len(rows), "comp": comp_counts}
