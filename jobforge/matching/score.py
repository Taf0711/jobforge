"""Explainable fit scoring: hard gates × weighted skill coverage."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from ..models import Job, MatchResult, Preferences, Profile
from .economics import comp_policy, is_nyc_role, location_economics
from .experience import resolve_years
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
SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|architect|manager|director|vp|head)\b", re.IGNORECASE)
INTERN_TITLE = re.compile(r"\b(intern|internship|co-op|coop)\b", re.IGNORECASE)
ENTRY_TITLE = re.compile(
    r"\b(?:new\s+grad(?:uate)?|college\s+grad(?:uate)?|early\s+career|"
    r"entry[- ]level|grad(?:uate)?)\b",
    re.IGNORECASE,
)


def _profile_degree(profile: Profile) -> str:
    best = "none"
    for edu in profile.education:
        value = str(edu.get("degree", "")).lower()
        if "ph.d" in value or "phd" in value or "doctor" in value:
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


def _tri_bool(value: Any) -> bool | None:
    """Return True/False for an explicit value, or None when unset/unknown."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "yes", "1", "y"}:
        return True
    if text in {"false", "no", "0", "n"}:
        return False
    return None


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


def _role_is_remote(job: Job, jd: JD) -> bool:
    # Existing rows can carry a stale remote=True flag from a company blurb.
    # Trust it only when the stored location is actually remote/empty; otherwise
    # use the fixed location-aware parser result.
    if jd.remote:
        return True
    loc = (job.location or "").lower()
    remote_markers = ("remote", "work from home", "distributed", "anywhere", "worldwide", "global")
    return bool(job.remote and (not loc or any(token in loc for token in remote_markers)))


US_REMOTE_MARKERS = (
    "united states", "usa", "u.s.", "us only", "remote us", "remote - us",
    "remote (us)", "america", "new york", "san francisco", "seattle", "boston",
    "austin", "chicago", "denver", "washington", "atlanta", "miami",
)
NON_US_REMOTE_MARKERS = (
    "india", "brazil", "canada", "mexico", "argentina", "united kingdom", " uk",
    "london", "europe", "emea", "apac", "australia", "germany", "france", "spain",
    "portugal", "lisbon", "singapore", "japan", "tokyo", "korea", "china", "poland",
    "ireland", "dublin", "netherlands", "amsterdam", "sweden", "switzerland",
    "israel", "tel aviv", "dubai", "uae", "south africa", "new zealand",
    "philippines", "nigeria", "kenya", "pakistan", "bangladesh", "sri lanka",
    "nepal", "colombia", "costa rica",
)


def _remote_scope_allowed(location: str, prefs: Preferences) -> bool:
    loc = _normalized_location(location)
    if not loc:
        return True
    us = any(marker in loc for marker in US_REMOTE_MARKERS)
    non_us = any(marker in loc for marker in NON_US_REMOTE_MARKERS)
    if us:
        return True
    if not non_us:
        return True
    # A candidate can explicitly target a non-US location.
    return any(
        any(marker in _normalized_location(wanted) for marker in NON_US_REMOTE_MARKERS)
        for wanted in prefs.locations
    )


def _normalized_location(value: str) -> str:
    text = (value or "").lower().strip()
    replacements = (
        ("new york city", "new york"),
        ("nyc", "new york"),
        ("san francisco bay area", "san francisco"),
        ("bay area", "san francisco"),
        ("sf", "san francisco"),
        ("washington, d.c.", "washington"),
        ("washington dc", "washington"),
    )
    for old, new in replacements:
        text = text.replace(old, new)
    return text


def _location_gate(
    job: Job, jd: JD, prefs: Preferences
) -> tuple[float, float, str, str]:
    """Return (fit multiplier, relocation fit, reason, gap).

    Location is a logistics/risk signal, not a fit knockout. The fit multiplier
    stays 1.0; the relocation fit is passed to the cost-of-living pursuit
    modifier. This mirrors resume-forge's gate model.
    """
    remote = _role_is_remote(job, jd)
    if remote:
        if not _remote_scope_allowed(job.location or jd.location, prefs):
            return 1.0, 0.25, "", f"Remote scope outside your location preferences: {job.location}"
        remote_wanted = (
            not prefs.locations
            or any("remote" in str(item).lower() for item in prefs.locations)
            or any("remote" in str(item).lower() for item in prefs.work_modes)
        )
        if remote_wanted:
            return 1.0, 1.0, "Remote role", ""
        return 1.0, 0.6, "Remote role", "Remote role outside your work-mode preference"
    if not prefs.locations:
        return 1.0, 1.0, "No location preference", ""
    job_loc = _normalized_location(job.location or jd.location)
    if not job_loc:
        return 1.0, 0.8, "Location not listed", "Location not listed"
    for wanted in prefs.locations:
        wanted = _normalized_location(wanted)
        if not wanted:
            continue
        if wanted in job_loc or job_loc in wanted:
            return 1.0, 1.0, f"Location match: {job.location}", ""
    if prefs.open_to_relocation:
        # Location is a logistics signal, not a fit knockout. It belongs in the
        # reasons so the human sees it, but it must not masquerade as a skill gap.
        return 1.0, 0.6, f"Location outside preferred; relocation possible: {job.location}", ""
    return 1.0, 0.4, f"Location outside preferred: {job.location}", ""


TITLE_GENERIC = {
    "engineer", "engineering", "software", "developer", "development", "senior", "junior",
    "staff", "principal", "lead", "manager", "director", "intern", "internship", "analyst",
    "associate", "specialist", "remote", "platform", "product", "data", "backend", "frontend",
    "fullstack", "full-stack", "i", "ii", "iii", "and", "the", "of", "at", "for",
}
HEAD_NOUNS = {
    "engineer", "engineering", "developer", "analyst", "scientist", "designer",
    "manager", "architect", "technical", "amts", "mts",
}
SOFTWARE_TITLE_TOKENS = {
    "software", "backend", "frontend", "fullstack", "full-stack", "data", "platform",
    "infrastructure", "systems", "security", "cloud", "devops", "site", "reliability",
    "mobile", "embedded", "web", "developer", "machine", "learning", "ai", "api",
    "engineering", "technical", "amts", "mts",
}


def _title_gate(
    job: Job, prefs: Preferences, profile: Profile | None = None
) -> tuple[float, str, str]:
    title = job.title.lower()
    wanted_titles = list(prefs.titles)
    levels = {level.lower() for level in prefs.experience_levels}
    if profile is not None:
        if not wanted_titles:
            wanted_titles.extend(
                str(entry.get("title") or "").strip()
                for entry in (profile.experience or [])[:2]
                if str(entry.get("title") or "").strip()
            )
            if profile.headline.strip():
                wanted_titles.append(profile.headline.strip())
        if not levels:
            hint = " ".join(
                [profile.headline]
                + [str(entry.get("title") or "") for entry in (profile.experience or [])[:2]]
            ).lower()
            if re.search(r"\b(new\s+grad(?:uate)?|entry[- ]level|junior|intern(?:ship)?)\b", hint):
                levels = {"entry", "junior"}
    wants_intern = any("intern" in t.lower() for t in levels)
    if INTERN_TITLE.search(title) and not wants_intern:
        return (
            0.50,
            "Internship title outside your target levels",
            "Title level mismatch",
        )
    if SENIOR_TITLE.search(title) and ({"entry", "junior", "internship"} & levels):
        return 0.50, "Senior title vs. entry/junior target", "Title level mismatch"
    if not wanted_titles:
        return 1.0, "", ""
    for wanted in wanted_titles:
        if wanted.lower().strip() in title:
            return 1.0, f"Title alignment: {job.title}", ""
    title_tokens = {t.lower() for t in re.findall(r"[a-z0-9+#.]+", title) if len(t) > 2}
    for wanted in wanted_titles:
        wanted_tokens = {
            t.lower()
            for t in re.findall(r"[a-z0-9+#.]+", wanted)
            if len(t) > 2 and t.lower() not in TITLE_GENERIC
        }
        if wanted_tokens and wanted_tokens & title_tokens:
            return 1.0, f"Title alignment: {job.title}", ""
    if title_tokens & HEAD_NOUNS and title_tokens & SOFTWARE_TITLE_TOKENS:
        if ENTRY_TITLE.search(title) and (not levels or {"entry", "junior"} & levels):
            return 1.0, f"Title alignment: entry-level {job.title}", ""
        return 0.85, f"Title is adjacent to your targets: {job.title}", ""
    reason = f"Title is outside your target families: {job.title}"
    return 0.50, reason, "Title family mismatch"


EVIDENCE_LEVEL_WEIGHTS = {
    "direct_professional": 1.0,
    "direct_project": 0.8,
    "adjacent_professional": 0.7,
    "adjacent_project_coursework": 0.55,
    "skills_list_only": 0.3,
    "interest_only": 0.1,
    "no_evidence": 0.0,
}


def _evidence(profile: Profile) -> dict[str, float]:
    """Best evidence weight per skill.

    The profile's curated evidence matrix is authoritative when present: a
    skill can be marked direct/adjacent even when the one-line resume did not
    spell it out. Facts then raise the floor, and the plain skills list is the
    weakest signal rather than an automatic 0.4.
    """
    out: dict[str, float] = {}
    for skill in profile.skills:
        out[skill] = max(out.get(skill, 0.0), 0.40)  # skills-list-only evidence
    matrix = profile.evidence or {}
    for group in (matrix.get("skills") or {}).values():
        if not isinstance(group, dict):
            continue
        for skill, entry in group.items():
            if not isinstance(entry, dict):
                continue
            level = str(entry.get("best_level") or "").strip().lower()
            weight = EVIDENCE_LEVEL_WEIGHTS.get(level)
            if weight is not None:
                out[str(skill)] = max(out.get(str(skill), 0.0), weight)
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
    *,
    scoring_v2: bool = True,
) -> MatchResult:
    graph = graph or GRAPH
    # Only the description is requirement evidence. Titles, company names, and
    # locations may contain skill-like tokens (for example a company named
    # Datadog or a title containing Machine Learning) but are not requirements.
    jd = parse_jd(
        job.description or "",
        title_hint=job.title,
        company_hint=job.company,
        location_hint=job.location,
    )

    coverage_result = coverage(
        jd.requirements,
        _all_profile_skills(profile),
        graph,
        _evidence(profile),
        prose_credit=0.0 if scoring_v2 else 0.60,
        evidence_direct=scoring_v2,
    )

    gates: list[tuple[float, str, str]] = []  # (survival, reason, gap)
    years = resolve_years(profile, prefs)
    years_have = years.value
    years_meta: dict[str, Any] = {
        "have": years_have,
        "required": jd.years_required,
        "source": years.source,
        "status": "not_required",
    }
    years_reason = ""
    years_gap = ""
    if jd.years_required is not None:
        if years_have is None:
            years_meta["status"] = "unknown"
            years_gap = (
                f"Requires {jd.years_required:g}+ years; your years are unknown "
                "— set them in Settings or add dated experience"
            )
        else:
            delta = jd.years_required - years_have
            have_label = f"{years_have:g}"
            if years.source == "experience":
                have_label += " (estimated from dated roles)"
            if delta >= 4:
                gates.append(
                    (0.0, "", f"Requires {jd.years_required:g}+ years; profile has {have_label}")
                )
                years_meta["status"] = "far_short"
            elif delta >= 2:
                gates.append(
                    (0.20, "", f"Requires {jd.years_required:g}+ years; profile has {have_label}")
                )
                years_meta["status"] = "short"
            elif delta > 0:
                gates.append((0.65, f"Close on experience ({jd.years_required:g}+ required)", ""))
                years_meta["status"] = "close"
            else:
                gates.append((1.0, f"Experience clears {jd.years_required:g}+ year floor", ""))
                years_meta["status"] = "clears"
    elif jd.years_mentioned:
        years_meta["status"] = "unparsed"
        years_gap = "Years requirement mentioned but not parsed; verify the posting"
    if (
        years.source == "experience"
        and years_have is not None
        and (jd.years_required is not None or jd.years_mentioned)
    ):
        years_reason = f"Experience estimate: {years_have:g} years from dated roles"

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
            eligible = _tri_bool(profile.work_auth.get("clearance_eligible"))
            if eligible is None:
                # Unknown is not ineligible. Keep the role scoreable and make
                # the missing authorization an explicit gap.
                gates.append(
                    (
                        0.35,
                        "",
                        f"Clearance required: {jd.clearance} (clearance eligibility not set)",
                    )
                )
            else:
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
        citizen = _tri_bool(profile.work_auth.get("us_citizen"))
        if citizen is None:
            gates.append(
                (
                    1.0,
                    "",
                    "US citizenship required; work authorization is not set",
                )
            )
        else:
            survival = citizenship_survival(True, citizen)
            gates.append(
                (
                    survival,
                    "US citizen — satisfies citizenship requirement" if survival >= 1.0 else "",
                    "US citizenship required" if survival < 1.0 else "",
                )
            )

    if jd.itar_required:
        eligible = _tri_bool(profile.work_auth.get("itar_eligible"))
        if eligible is None:
            gates.append(
                (
                    1.0,
                    "",
                    "ITAR eligibility required; work authorization is not set",
                )
            )
        else:
            survival = itar_survival(True, eligible)
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

    loc_gate, relocation_fit, loc_reason, loc_gap = _location_gate(job, jd, prefs)
    gates.append((loc_gate, loc_reason, loc_gap))

    title_gate, title_reason, title_gap = _title_gate(job, prefs, profile)
    gates.append((title_gate, title_reason, title_gap))

    gate_salary_min = job.salary_min if job.salary_min is not None else jd.salary_min
    gate_salary_max = job.salary_max if job.salary_max is not None else jd.salary_max
    if prefs.salary_min and gate_salary_max and gate_salary_max < prefs.salary_min:
        gates.append((0.80, "", f"Posted max ${gate_salary_max:,} below your ${prefs.salary_min:,} floor"))
    elif prefs.salary_min and gate_salary_min and gate_salary_min >= prefs.salary_min:
        gates.append((1.0, f"Salary band starts at ${gate_salary_min:,}", ""))

    survival = 1.0
    for value, _, _ in gates:
        survival *= value
    survival = max(0.0, min(1.0, survival))

    parsed_rows = [
        row
        for row in jd.requirements
        if str(row.get("importance", "")).lower() in {"must", "nice"}
        and (row.get("skills") or row.get("skill"))
    ]
    parsed_skills = [
        skill
        for row in parsed_rows
        for skill in (row.get("skills") or ([row["skill"]] if row.get("skill") else []))
        if skill
    ]
    sparse_jd = (
        scoring_v2
        and len(parsed_rows) < 2
        and len(parsed_skills) < 3
        and len(jd.requirements) > len(parsed_rows)
    )
    description = job.description or ""
    lowered = description.lower()
    lead_only = any(
        marker in lowered
        for marker in (
            "community job list",
            "open the employer posting",
        )
    )
    thin_jd = lead_only or not parsed_skills or sparse_jd
    score = round(100.0 * coverage_result.coverage * survival * (0.5 if thin_jd else 1.0), 1)
    reasons = [reason for _, reason, _ in gates if reason]
    gaps = [gap for _, _, gap in gates if gap] + coverage_result.gaps
    if years_reason:
        reasons.append(years_reason)
    if years_gap:
        gaps.insert(0, years_gap)
    if thin_jd:
        gaps.insert(0, "Job description is thin or lead-only; open the employer posting before trusting this fit")
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
        location_text, salary_mid, relocation_fit
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
        "location": {
            "modifier": location_mod,
            "notes": location_notes,
            "relocation_fit": relocation_fit,
        },
        "years": years_meta,
        "thin_jd": thin_jd,
        "sparse_jd": sparse_jd,
        "lead_only": lead_only,
        "word_count": len(description.split()),
        "parsed_skills": len(parsed_skills),
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
    *,
    only_unscored: bool = False,
    scoring_v2: bool = True,
) -> dict[str, Any]:
    from .. import db

    if only_unscored:
        sql = (
            "SELECT j.* FROM jobs j LEFT JOIN matches m "
            "ON m.job_id=j.id AND m.profile_id=? WHERE m.score IS NULL ORDER BY j.id"
        )
        params: list[Any] = [profile.id]
    else:
        sql = "SELECT * FROM jobs ORDER BY id"
        params = []
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    scored = 0
    comp_counts: dict[str, int] = {}
    for row in rows:
        job = db._job_from_row(row)
        result = score_job(job, profile, prefs, scoring_v2=scoring_v2)
        if job.id is not None:
            db.set_match(conn, result)
            scored += 1
            verdict = str(result.meta.get("comp", {}).get("verdict", "unknown"))
            comp_counts[verdict] = comp_counts.get(verdict, 0) + 1
    return {"scored": scored, "total": len(rows), "comp": comp_counts}
