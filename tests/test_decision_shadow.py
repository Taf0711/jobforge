from __future__ import annotations

import json
from pathlib import Path

from jobforge.automation.decision_model import (
    ActionKind,
    ButtonObservation,
    Decision,
    FieldCategory,
    FieldObservation,
    Observation,
    PageKind,
    ValueSource,
)
from jobforge.automation.decision_shadow import (
    decision_mode,
    read_shadow,
    record_shadow,
    shadow_enabled,
)


def _observation() -> Observation:
    return Observation(
        page_kind=PageKind.APPLICATION_FORM,
        ats="ashby",
        fields=(
            FieldObservation(
                field_id="email",
                label="Email",
                kind="email",
                required=True,
                category=FieldCategory.CONTACT,
                has_grounded_answer=True,
                answer_value="secret@example.com",
                value_source=ValueSource.PROFILE,
            ),
        ),
        buttons=(ButtonObservation("submit", "Submit application", is_submit=True),),
        quality_ready=True,
        risk_allowed=True,
    )


def _decision() -> Decision:
    return Decision(
        action=ActionKind.FILL_TEXT,
        target="email",
        value="secret@example.com",
        value_source=ValueSource.PROFILE,
        rationale="test",
        policy_rule="grounded_fill",
    )


def test_shadow_mode_is_off_by_default_and_active_fails_closed(monkeypatch):
    monkeypatch.delenv("JOBFORGE_DECISION_MODEL", raising=False)
    assert decision_mode() == "off"
    assert shadow_enabled() is False
    monkeypatch.setenv("JOBFORGE_DECISION_MODEL", "active")
    assert decision_mode() == "off"
    assert shadow_enabled() is False
    monkeypatch.setenv("JOBFORGE_DECISION_MODEL", "shadow")
    assert decision_mode() == "shadow"
    assert shadow_enabled() is True


def test_shadow_record_redacts_values_and_is_private(tmp_path: Path):
    target = tmp_path / "decision_shadow.jsonl"
    observation = _observation()
    decision = _decision()
    record_shadow(target, observation, decision, run_id=77, job_id=4136)

    raw = target.read_text(encoding="utf-8")
    assert "secret@example.com" not in raw
    assert "<redacted>" in raw
    assert target.stat().st_mode & 0o777 == 0o600
    records = read_shadow(target)
    assert len(records) == 1
    item = records[0]
    assert item["run_id"] == 77
    assert item["job_id"] == 4136
    assert item["decision"]["action"] == "fill_text"
    assert item["observation"]["fields"][0]["answer_value"] == "<redacted>"
    json.dumps(item)  # serializable


def test_read_shadow_ignores_partial_lines(tmp_path: Path):
    target = tmp_path / "decision_shadow.jsonl"
    target.write_text('{"ts": "one"}\nnot-json\n', encoding="utf-8")
    assert read_shadow(target) == [{"ts": "one"}]
