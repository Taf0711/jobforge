from __future__ import annotations

import pytest

from jobforge.automation.decision_model import (
    ActionKind,
    ButtonObservation,
    Decision,
    DecisionError,
    FieldCategory,
    FieldObservation,
    Observation,
    Outcome,
    PageKind,
    TrajectoryAction,
    TrajectoryState,
    ValueSource,
    decide,
    evaluate_trajectory,
)


def field(
    field_id: str = "f1",
    label: str = "Field",
    *,
    kind: str = "text",
    required: bool = False,
    value: str = "",
    category: FieldCategory = FieldCategory.CONTACT,
    sensitive: bool = False,
    grounded: bool = False,
    answer: str = "",
    source: ValueSource = ValueSource.NONE,
    visible: bool = True,
) -> FieldObservation:
    return FieldObservation(
        field_id=field_id,
        label=label,
        kind=kind,
        required=required,
        value=value,
        category=category,
        sensitive=sensitive,
        has_grounded_answer=grounded,
        answer_value=answer,
        value_source=source,
        visible=visible,
    )


def observation(**overrides) -> Observation:
    values = {
        "page_kind": PageKind.APPLICATION_FORM,
        "ats": "ashby",
        "url": "https://jobs.ashbyhq.com/n1/example/application",
        "fields": (
            field("name", "Full name", required=True, grounded=True, answer="Tafseer Haque", source=ValueSource.PROFILE),
        ),
        "buttons": (ButtonObservation("next", "Next", is_next=True),),
    }
    values.update(overrides)
    return Observation(**values)


def test_captcha_stops_before_any_fill():
    decision = decide(observation(captcha=True))
    assert decision.action is ActionKind.STOP_CAPTCHA
    assert decision.requires_human is True
    assert decision.policy_rule == "captcha"


def test_account_wall_stops():
    decision = decide(observation(account_wall=True))
    assert decision.action is ActionKind.STOP_ACCOUNT_WALL
    assert decision.requires_human is True


def test_quality_gate_stops():
    decision = decide(observation(quality_ready=False))
    assert decision.action is ActionKind.STOP_QUALITY_GATE
    assert decision.policy_rule == "quality_gate"


def test_sensitive_required_without_answer_requests_review():
    decision = decide(
        observation(
            fields=(
                field(
                    "veteran",
                    "Veteran status",
                    kind="select",
                    required=True,
                    category=FieldCategory.DEMOGRAPHIC,
                    sensitive=True,
                ),
            )
        )
    )
    assert decision.action is ActionKind.REQUEST_REVIEW
    assert decision.target == "veteran"
    assert decision.requires_human is True
    assert decision.policy_rule == "required_review"


def test_motivation_required_without_answer_requests_review():
    decision = decide(
        observation(
            fields=(
                field(
                    "why",
                    "Why N1?",
                    kind="textarea",
                    required=True,
                    category=FieldCategory.MOTIVATION,
                ),
            )
        )
    )
    assert decision.action is ActionKind.REQUEST_REVIEW
    assert decision.target == "why"
    assert "do not guess" in decision.rationale


def test_grounded_required_field_fills():
    decision = decide(observation())
    assert decision.action is ActionKind.FILL_TEXT
    assert decision.target == "name"
    assert decision.value_source is ValueSource.PROFILE
    assert decision.to_dict()["value"] == "<redacted>"


def test_optional_grounded_field_fills_before_next():
    decision = decide(
        observation(
            fields=(
                field("name", "Full name", required=True, value="Tafseer Haque"),
                field(
                    "site",
                    "Website",
                    grounded=True,
                    answer="https://example.com",
                    source=ValueSource.PROFILE,
                ),
            )
        )
    )
    assert decision.action is ActionKind.FILL_TEXT
    assert decision.target == "site"


def test_submit_requires_complete_ready_form():
    decision = decide(
        observation(
            fields=(field("name", "Full name", required=True, value="Tafseer Haque"),),
            buttons=(ButtonObservation("submit", "Submit application", is_submit=True),),
        )
    )
    assert decision.action is ActionKind.SUBMIT
    assert decision.target == "submit"
    decision.validate_against(
        observation(
            fields=(field("name", "Full name", required=True, value="Tafseer Haque"),),
            buttons=(ButtonObservation("submit", "Submit application", is_submit=True),),
        )
    )


def test_submit_is_blocked_when_quality_gate_is_not_ready():
    obs = observation(
        fields=(field("name", "Full name", required=True, value="Tafseer Haque"),),
        buttons=(ButtonObservation("submit", "Submit application", is_submit=True),),
        quality_ready=False,
    )
    decision = decide(obs)
    assert decision.action is ActionKind.STOP_QUALITY_GATE


def test_submit_decision_rejects_missing_required_field():
    obs = observation(
        fields=(field("name", "Full name", required=True, value=""),),
        buttons=(ButtonObservation("submit", "Submit application", is_submit=True),),
    )
    decision = Decision(
        action=ActionKind.SUBMIT,
        target="submit",
        rationale="test",
        policy_rule="test",
    )
    with pytest.raises(DecisionError, match="required fields missing"):
        decision.validate_against(obs)


def test_model_draft_cannot_be_filled():
    with pytest.raises(DecisionError, match="grounded value source"):
        Decision(
            action=ActionKind.FILL_TEXT,
            target="why",
            value="I love the mission.",
            value_source=ValueSource.MODEL_DRAFT,
            rationale="test",
            policy_rule="test",
        )


def test_unknown_page_abstains():
    decision = decide(observation(page_kind=PageKind.UNKNOWN))
    assert decision.action is ActionKind.ABSTAIN
    assert decision.requires_human is True


def test_stuck_validation_asks_human():
    decision = decide(observation(validation_errors=("Invalid email",), attempt=2))
    assert decision.action is ActionKind.ASK_HUMAN
    assert decision.policy_rule == "stuck_validation"


def test_state_hash_changes_with_field_value():
    first = observation()
    second = observation(
        fields=(
            field("name", "Full name", required=True, value="Tafseer Haque"),
        )
    )
    assert first.state_hash != second.state_hash


def test_trajectory_captcha_stops():
    decision = evaluate_trajectory(
        [
            TrajectoryState(
                attempt=0,
                step=3,
                state_hash="abc",
                page_kind=PageKind.CAPTCHA,
                captcha=True,
            )
        ]
    )
    assert decision.action is TrajectoryAction.STOP_CAPTCHA
    assert decision.requires_human is True


def test_trajectory_done_on_success_page():
    decision = evaluate_trajectory(
        [TrajectoryState(attempt=0, step=4, state_hash="done", page_kind=PageKind.SUCCESS)]
    )
    assert decision.action is TrajectoryAction.DONE


def test_trajectory_oscillation_stops():
    history = [
        TrajectoryState(attempt=0, step=1, state_hash="a", page_kind=PageKind.APPLICATION_FORM, required_missing=1),
        TrajectoryState(attempt=0, step=2, state_hash="b", page_kind=PageKind.APPLICATION_FORM, required_missing=1),
        TrajectoryState(attempt=0, step=3, state_hash="a", page_kind=PageKind.APPLICATION_FORM, required_missing=1),
        TrajectoryState(attempt=0, step=4, state_hash="b", page_kind=PageKind.APPLICATION_FORM, required_missing=1),
    ]
    decision = evaluate_trajectory(history)
    assert decision.action is TrajectoryAction.STOP_STUCK
    assert decision.rule == "oscillation"


def test_trajectory_no_progress_stops():
    history = [
        TrajectoryState(attempt=0, step=i, state_hash=f"s{i}", page_kind=PageKind.APPLICATION_FORM, required_missing=1)
        for i in range(1, 4)
    ]
    decision = evaluate_trajectory(history)
    assert decision.action is TrajectoryAction.STOP_STUCK
    assert decision.rule == "no_progress"


def test_trajectory_confidence_decline_asks_human():
    history = [
        TrajectoryState(
            attempt=0,
            step=i,
            state_hash=f"s{i}",
            page_kind=PageKind.APPLICATION_FORM,
            required_missing=0,
            confidence=confidence,
            last_outcome=Outcome.RETRY,
        )
        for i, confidence in enumerate((0.9, 0.7, 0.5), start=1)
    ]
    decision = evaluate_trajectory(history)
    assert decision.action is TrajectoryAction.ASK_HUMAN
    assert decision.rule == "confidence_decline"


def test_motivation_answer_bank_is_fillable():
    decision = decide(
        observation(
            fields=(
                field(
                    "why",
                    "Why N1?",
                    kind="textarea",
                    required=True,
                    category=FieldCategory.MOTIVATION,
                    grounded=True,
                    answer="Approved answer.",
                    source=ValueSource.ANSWER_BANK,
                ),
            )
        )
    )
    assert decision.action is ActionKind.FILL_TEXT
    assert decision.value_source is ValueSource.ANSWER_BANK


def test_salary_from_profile_requires_review():
    decision = decide(
        observation(
            fields=(
                field(
                    "salary",
                    "Desired salary",
                    required=True,
                    category=FieldCategory.SALARY,
                    grounded=True,
                    answer="130000",
                    source=ValueSource.PROFILE,
                ),
            )
        )
    )
    assert decision.action is ActionKind.REQUEST_REVIEW


def test_sensitive_answer_bank_override_requires_review():
    decision = decide(
        observation(
            fields=(
                field(
                    "veteran",
                    "Veteran status",
                    required=True,
                    category=FieldCategory.DEMOGRAPHIC,
                    sensitive=True,
                    grounded=True,
                    answer="No",
                    source=ValueSource.ANSWER_BANK,
                ),
            )
        )
    )
    assert decision.action is ActionKind.REQUEST_REVIEW


def test_sensitive_profile_fact_can_fill():
    decision = decide(
        observation(
            fields=(
                field(
                    "clearance",
                    "Do you hold an active clearance?",
                    required=True,
                    category=FieldCategory.CLEARANCE,
                    grounded=True,
                    answer="No",
                    source=ValueSource.PROFILE,
                ),
            )
        )
    )
    assert decision.action is ActionKind.FILL_TEXT


def test_unknown_answer_bank_override_requires_review():
    decision = decide(
        observation(
            fields=(
                field(
                    "weird",
                    "Describe your ideal Tuesday",
                    kind="textarea",
                    required=True,
                    category=FieldCategory.UNKNOWN,
                    grounded=True,
                    answer="A quiet one.",
                    source=ValueSource.ANSWER_BANK,
                ),
            )
        )
    )
    assert decision.action is ActionKind.REQUEST_REVIEW


def test_grounded_fill_runs_before_review_gap():
    obs = observation(
        fields=(
            field(
                "resume",
                "Resume",
                kind="file",
                required=True,
                category=FieldCategory.FILE,
                grounded=True,
                answer="/tmp/resume.pdf",
                source=ValueSource.RESUME,
            ),
            field(
                "why",
                "Why N1?",
                kind="textarea",
                required=True,
                category=FieldCategory.MOTIVATION,
            ),
        ),
        buttons=(ButtonObservation("submit", "Submit application", is_submit=True),),
    )
    first = decide(obs)
    assert first.action is ActionKind.UPLOAD_FILE
    assert first.target == "resume"

    second = decide(
        observation(
            fields=(
                field(
                    "resume",
                    "Resume",
                    kind="file",
                    required=True,
                    category=FieldCategory.FILE,
                    value="/tmp/resume.pdf",
                    grounded=True,
                    answer="/tmp/resume.pdf",
                    source=ValueSource.RESUME,
                ),
                field(
                    "why",
                    "Why N1?",
                    kind="textarea",
                    required=True,
                    category=FieldCategory.MOTIVATION,
                ),
            ),
            buttons=(ButtonObservation("submit", "Submit application", is_submit=True),),
        )
    )
    assert second.action is ActionKind.REQUEST_REVIEW
    assert second.target == "why"


def test_filled_model_draft_required_requests_review():
    decision = decide(
        observation(
            fields=(
                field(
                    "why",
                    "Why N1?",
                    kind="textarea",
                    required=True,
                    category=FieldCategory.MOTIVATION,
                    value="A draft that was never approved.",
                    grounded=True,
                    answer="A draft that was never approved.",
                    source=ValueSource.MODEL_DRAFT,
                ),
            ),
            buttons=(ButtonObservation("submit", "Submit application", is_submit=True),),
        )
    )
    assert decision.action is ActionKind.REQUEST_REVIEW
    assert decision.target == "why"


def test_filled_sensitive_answer_bank_required_requests_review():
    decision = decide(
        observation(
            fields=(
                field(
                    "gender",
                    "Gender",
                    kind="select",
                    required=True,
                    category=FieldCategory.DEMOGRAPHIC,
                    sensitive=True,
                    value="No",
                    grounded=True,
                    answer="No",
                    source=ValueSource.ANSWER_BANK,
                ),
            ),
            buttons=(ButtonObservation("submit", "Submit application", is_submit=True),),
        )
    )
    assert decision.action is ActionKind.REQUEST_REVIEW
    assert decision.target == "gender"


def test_stale_posting_stops():
    decision = decide(observation(job_age_days=31))
    assert decision.action is ActionKind.STOP_STALE
    assert decision.policy_rule == "stale_posting"


def test_fresh_posting_is_not_stopped():
    decision = decide(observation(job_age_days=29))
    assert decision.action is not ActionKind.STOP_STALE


def test_trajectory_stale_posting_stops():
    decision = evaluate_trajectory(
        [
            TrajectoryState(
                attempt=0,
                step=1,
                state_hash="stale",
                page_kind=PageKind.APPLICATION_FORM,
                job_age_days=45,
            )
        ]
    )
    assert decision.action is TrajectoryAction.STOP_STALE
    assert decision.rule == "stale_posting"
