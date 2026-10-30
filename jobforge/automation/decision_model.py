"""Typed decision contract for the autonomous application loop.

This module is the formulation layer: observation -> decision -> action, with
deterministic safety rules first and model-backed classification only as a
replacement for the ambiguous middle. It is intentionally not wired into the
browser yet. The first use is offline replay and shadow evaluation.

Design constraints
------------------
- A decision names one action from a closed set; free-form values are separate
  arguments.
- Deterministic guards outrank every classifier. Captcha, 2FA, account walls,
  quality gates, and risk gates are terminal before any model is consulted.
- ``submit`` can never be authorized by a classifier alone. It requires an
  explicit application form, a ready quality gate, an allowed risk gate, no
  missing required fields, and no unresolved validation errors.
- Sensitive disclosures and motivation answers are never guessed. They become
  review items with ``requires_human=True``.
- A model may draft an answer, but a draft cannot be filled. It must be approved
  and stored in the answer bank first.
- The observation is transient. Trace records should redact values and keep the
  action, rule, confidence, and state hash.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class PageKind(str, Enum):
    JOB_LIST = "job_list"
    JOB_DETAIL = "job_detail"
    APPLICATION_FORM = "application_form"
    FORM_REVIEW = "form_review"
    SUCCESS = "success"
    CAPTCHA = "captcha"
    LOGIN = "login"
    ACCOUNT_WALL = "account_wall"
    CLOSED = "closed"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


class FieldKind(str, Enum):
    TEXT = "text"
    EMAIL = "email"
    TEL = "tel"
    URL = "url"
    NUMBER = "number"
    TEXTAREA = "textarea"
    SELECT = "select"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    FILE = "file"
    COMBOBOX = "combobox"
    DATE = "date"


class FieldCategory(str, Enum):
    IDENTITY = "identity"
    CONTACT = "contact"
    LOCATION = "location"
    WORK_AUTHORIZATION = "work_authorization"
    SPONSORSHIP = "sponsorship"
    EDUCATION = "education"
    EXPERIENCE = "experience"
    MOTIVATION = "motivation"
    DEMOGRAPHIC = "demographic"
    SALARY = "salary"
    CLEARANCE = "clearance"
    CONSENT = "consent"
    FILE = "file"
    UNKNOWN = "unknown"


SENSITIVE_CATEGORIES = frozenset(
    {
        FieldCategory.DEMOGRAPHIC,
        FieldCategory.SALARY,
        FieldCategory.CLEARANCE,
        FieldCategory.CONSENT,
    }
)


class ValueSource(str, Enum):
    NONE = "none"
    PROFILE = "profile"
    ANSWER_BANK = "answer_bank"
    JOB = "job"
    RESUME = "resume"
    HUMAN = "human"
    MODEL_DRAFT = "model_draft"


MAX_JOB_AGE_DAYS = 30

GROUNDED_SOURCES = frozenset(
    {ValueSource.PROFILE, ValueSource.ANSWER_BANK, ValueSource.JOB, ValueSource.RESUME, ValueSource.HUMAN}
)
# Motivation, salary, and consent are review-only unless a human approved the
# exact answer into the answer bank. A profile preference is not enough.
REVIEW_ONLY_CATEGORIES = frozenset(
    {FieldCategory.MOTIVATION, FieldCategory.SALARY, FieldCategory.CONSENT}
)
REVIEW_ONLY_SOURCES = frozenset({ValueSource.ANSWER_BANK, ValueSource.HUMAN})
UNKNOWN_SOURCES = frozenset({ValueSource.PROFILE, ValueSource.HUMAN})
# Legal/voluntary disclosures can only come from an explicit profile fact or a
# human, never from a loose answer-bank override.
SENSITIVE_SOURCES = frozenset({ValueSource.PROFILE, ValueSource.HUMAN})


class ActionKind(str, Enum):
    FILL_TEXT = "fill_text"
    SELECT_OPTION = "select_option"
    CHECK_BOX = "check_box"
    UPLOAD_FILE = "upload_file"
    CLEAR_FIELD = "clear_field"
    CLICK_NEXT = "click_next"
    CLICK_BACK = "click_back"
    SCROLL = "scroll"
    WAIT = "wait"
    NAVIGATE = "navigate"
    DRAFT_ANSWER = "draft_answer"
    REQUEST_REVIEW = "request_review"
    ASK_HUMAN = "ask_human"
    SUBMIT = "submit"
    DONE = "done"
    STOP_CAPTCHA = "stop_captcha"
    STOP_2FA = "stop_2fa"
    STOP_ACCOUNT_WALL = "stop_account_wall"
    STOP_UNSUPPORTED = "stop_unsupported"
    STOP_CLOSED = "stop_closed"
    STOP_STALE = "stop_stale"
    STOP_QUALITY_GATE = "stop_quality_gate"
    STOP_RISK_GATE = "stop_risk_gate"
    ABSTAIN = "abstain"


FILL_ACTIONS = frozenset(
    {ActionKind.FILL_TEXT, ActionKind.SELECT_OPTION, ActionKind.CHECK_BOX, ActionKind.UPLOAD_FILE}
)
TERMINAL_ACTIONS = frozenset(
    {
        ActionKind.DONE,
        ActionKind.STOP_CAPTCHA,
        ActionKind.STOP_2FA,
        ActionKind.STOP_ACCOUNT_WALL,
        ActionKind.STOP_UNSUPPORTED,
        ActionKind.STOP_CLOSED,
        ActionKind.STOP_STALE,
        ActionKind.STOP_QUALITY_GATE,
        ActionKind.STOP_RISK_GATE,
        ActionKind.ABSTAIN,
    }
)


class Outcome(str, Enum):
    SUCCESS = "success"
    RETRY = "retry"
    BLOCKED = "blocked"
    FAILED = "failed"
    UNKNOWN = "unknown"


class DecisionError(ValueError):
    """Raised when an observation or decision violates the contract."""


@dataclass(frozen=True)
class FieldObservation:
    field_id: str
    label: str
    kind: FieldKind = FieldKind.TEXT
    required: bool = False
    visible: bool = True
    enabled: bool = True
    options: tuple[str, ...] = ()
    value: str = ""
    category: FieldCategory = FieldCategory.UNKNOWN
    sensitive: bool = False
    has_grounded_answer: bool = False
    answer_value: str = ""
    value_source: ValueSource = ValueSource.NONE
    error: str = ""

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "kind", FieldKind(self.kind))
        except ValueError as exc:
            raise DecisionError(f"unknown field kind: {self.kind!r}") from exc

    @property
    def filled(self) -> bool:
        return bool(self.value.strip())

    @property
    def actionable(self) -> bool:
        return self.visible and self.enabled and not self.error.strip()

    @property
    def is_sensitive(self) -> bool:
        return self.sensitive or self.category in SENSITIVE_CATEGORIES

    @property
    def allowed_sources(self) -> frozenset[ValueSource]:
        if self.category in REVIEW_ONLY_CATEGORIES:
            return REVIEW_ONLY_SOURCES
        if self.category is FieldCategory.UNKNOWN:
            return UNKNOWN_SOURCES
        if self.is_sensitive:
            return SENSITIVE_SOURCES
        return GROUNDED_SOURCES

    @property
    def grounded_for_fill(self) -> bool:
        return (
            self.has_grounded_answer
            and self.value_source in self.allowed_sources
            and bool(self.answer_value.strip())
        )

    @property
    def needs_review(self) -> bool:
        if not (self.required and self.actionable):
            return False
        if self.filled:
            # A human-typed value has no provenance yet and is allowed. A model
            # draft, or a sensitive/unknown value from a source that is not
            # approved for that category, is not.
            if self.value_source is ValueSource.MODEL_DRAFT:
                return True
            return (
                self.value_source is not ValueSource.NONE
                and self.value_source not in self.allowed_sources
            )
        return not self.grounded_for_fill

    @property
    def can_fill(self) -> bool:
        return self.actionable and not self.filled and self.grounded_for_fill

    @property
    def answer_action(self) -> ActionKind:
        if self.kind in {FieldKind.SELECT, FieldKind.RADIO, FieldKind.COMBOBOX}:
            return ActionKind.SELECT_OPTION
        if self.kind is FieldKind.CHECKBOX:
            return ActionKind.CHECK_BOX
        if self.kind is FieldKind.FILE:
            return ActionKind.UPLOAD_FILE
        return ActionKind.FILL_TEXT

    def to_dict(self) -> dict[str, object]:
        return {
            "field_id": self.field_id,
            "label": self.label,
            "kind": self.kind.value,
            "required": self.required,
            "visible": self.visible,
            "enabled": self.enabled,
            "options": list(self.options),
            "value": self.value,
            "category": self.category.value,
            "sensitive": self.sensitive,
            "has_grounded_answer": self.has_grounded_answer,
            "answer_value": self.answer_value,
            "value_source": self.value_source.value,
            "error": self.error,
        }


@dataclass(frozen=True)
class ButtonObservation:
    button_id: str
    label: str
    enabled: bool = True
    is_submit: bool = False
    is_next: bool = False
    is_back: bool = False

    @property
    def actionable(self) -> bool:
        return self.enabled and bool(self.button_id.strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "button_id": self.button_id,
            "label": self.label,
            "enabled": self.enabled,
            "is_submit": self.is_submit,
            "is_next": self.is_next,
            "is_back": self.is_back,
        }


@dataclass(frozen=True)
class ActionRecord:
    action: ActionKind
    target: str = ""
    outcome: Outcome = Outcome.UNKNOWN
    state_hash: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "target": self.target,
            "outcome": self.outcome.value,
            "state_hash": self.state_hash,
        }


@dataclass(frozen=True)
class Observation:
    page_kind: PageKind
    ats: str = ""
    url: str = ""
    step: int = 0
    fields: tuple[FieldObservation, ...] = ()
    buttons: tuple[ButtonObservation, ...] = ()
    validation_errors: tuple[str, ...] = ()
    captcha: bool = False
    two_factor: bool = False
    account_wall: bool = False
    quality_ready: bool = True
    risk_allowed: bool = True
    job_age_days: int | None = None
    application_id: str = ""
    attempt: int = 0
    confidence: float = 1.0
    state_hash: str = ""
    history: tuple[ActionRecord, ...] = ()

    def __post_init__(self) -> None:
        if self.step < 0:
            raise DecisionError("step must be >= 0")
        if self.attempt < 0:
            raise DecisionError("attempt must be >= 0")
        if not 0.0 <= self.confidence <= 1.0:
            raise DecisionError("confidence must be within [0, 1]")
        if self.job_age_days is not None and self.job_age_days < 0:
            raise DecisionError("job_age_days must be >= 0")
        # Hidden required fields are common on multi-step forms. They are not
        # actionable and are filtered by ``required_fields`` below; the adapter
        # decides whether a hidden control belongs to the current step.
        if not self.state_hash:
            object.__setattr__(self, "state_hash", self.compute_state_hash())

    @property
    def required_fields(self) -> tuple[FieldObservation, ...]:
        return tuple(item for item in self.fields if item.required and item.visible)

    @property
    def required_missing(self) -> tuple[FieldObservation, ...]:
        return tuple(item for item in self.required_fields if not item.filled)

    @property
    def required_review(self) -> tuple[FieldObservation, ...]:
        return tuple(item for item in self.required_fields if item.needs_review)

    @property
    def fillable(self) -> tuple[FieldObservation, ...]:
        return tuple(item for item in self.fields if item.can_fill)

    @property
    def submit_button(self) -> ButtonObservation | None:
        for item in self.buttons:
            if item.is_submit and item.actionable:
                return item
        return None

    @property
    def next_button(self) -> ButtonObservation | None:
        for item in self.buttons:
            if item.is_next and item.actionable:
                return item
        return None

    @property
    def back_button(self) -> ButtonObservation | None:
        for item in self.buttons:
            if item.is_back and item.actionable:
                return item
        return None

    @property
    def complete(self) -> bool:
        return (
            self.page_kind == PageKind.APPLICATION_FORM
            and not self.required_missing
            and not self.validation_errors
            and not self.required_review
        )

    def compute_state_hash(self) -> str:
        payload = {
            "page_kind": self.page_kind.value,
            "ats": self.ats,
            "url": self.url,
            "step": self.step,
            "fields": [
                (item.field_id, item.label, item.kind, item.required, item.value, item.error)
                for item in self.fields
            ],
            "buttons": [(item.button_id, item.label, item.enabled) for item in self.buttons],
            "errors": list(self.validation_errors),
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()[:16]

    def to_dict(self, *, include_values: bool = False) -> dict[str, object]:
        fields = []
        for item in self.fields:
            data = item.to_dict()
            if not include_values:
                data["value"] = "<redacted>" if data["value"] else ""
                data["answer_value"] = "<redacted>" if data["answer_value"] else ""
            fields.append(data)
        return {
            "page_kind": self.page_kind.value,
            "ats": self.ats,
            "url": self.url,
            "step": self.step,
            "fields": fields,
            "buttons": [item.to_dict() for item in self.buttons],
            "validation_errors": list(self.validation_errors),
            "captcha": self.captcha,
            "two_factor": self.two_factor,
            "account_wall": self.account_wall,
            "quality_ready": self.quality_ready,
            "risk_allowed": self.risk_allowed,
            "job_age_days": self.job_age_days,
            "application_id": self.application_id,
            "attempt": self.attempt,
            "confidence": self.confidence,
            "state_hash": self.state_hash,
            "history": [item.to_dict() for item in self.history],
        }


@dataclass(frozen=True)
class Decision:
    action: ActionKind
    target: str = ""
    value: str = ""
    value_source: ValueSource = ValueSource.NONE
    confidence: float = 1.0
    rationale: str = ""
    policy_rule: str = ""
    requires_human: bool = False
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise DecisionError("decision confidence must be within [0, 1]")
        if not self.rationale.strip():
            raise DecisionError("decision rationale is required")
        if self.action in FILL_ACTIONS:
            if not self.target.strip():
                raise DecisionError(f"{self.action.value} requires a target field")
            if not self.value.strip():
                raise DecisionError(f"{self.action.value} requires a value")
            if self.value_source not in GROUNDED_SOURCES:
                raise DecisionError(
                    f"{self.action.value} requires a grounded value source; "
                    "model drafts must be approved into the answer bank first"
                )
        if self.action is ActionKind.SUBMIT and not self.target.strip():
            raise DecisionError("submit requires an explicit target button")
        if self.action in TERMINAL_ACTIONS and not self.rationale.strip():
            raise DecisionError("terminal decisions need a rationale")

    def validate_against(self, observation: Observation) -> None:
        if self.action is not ActionKind.SUBMIT:
            return
        if observation.page_kind is not PageKind.APPLICATION_FORM:
            raise DecisionError("submit is only valid on an application form")
        if not observation.risk_allowed:
            raise DecisionError("submit blocked: risk gate is not satisfied")
        if not observation.quality_ready:
            raise DecisionError("submit blocked: quality gate is not satisfied")
        if observation.required_missing:
            labels = ", ".join(item.label for item in observation.required_missing)
            raise DecisionError(f"submit blocked: required fields missing: {labels}")
        if observation.validation_errors:
            raise DecisionError("submit blocked: unresolved validation errors")
        if observation.required_review:
            labels = ", ".join(item.label for item in observation.required_review)
            raise DecisionError(f"submit blocked: review fields unresolved: {labels}")

    def to_dict(self, *, include_value: bool = False) -> dict[str, object]:
        return {
            "action": self.action.value,
            "target": self.target,
            "value": self.value if include_value else ("<redacted>" if self.value else ""),
            "value_source": self.value_source.value,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "policy_rule": self.policy_rule,
            "requires_human": self.requires_human,
            "evidence": list(self.evidence),
        }


class TrajectoryAction(str, Enum):
    CONTINUE = "continue"
    RETRY = "retry"
    STEP_BACK = "step_back"
    ASK_HUMAN = "ask_human"
    STOP_CAPTCHA = "stop_captcha"
    STOP_2FA = "stop_2fa"
    STOP_ACCOUNT_WALL = "stop_account_wall"
    STOP_RISK = "stop_risk"
    STOP_STALE = "stop_stale"
    STOP_LIMIT = "stop_limit"
    STOP_STUCK = "stop_stuck"
    DONE = "done"


@dataclass(frozen=True)
class TrajectoryState:
    attempt: int
    step: int
    state_hash: str
    page_kind: PageKind
    fields_total: int = 0
    required_total: int = 0
    required_missing: int = 0
    validation_errors: int = 0
    captcha: bool = False
    two_factor: bool = False
    account_wall: bool = False
    risk_allowed: bool = True
    quality_ready: bool = True
    job_age_days: int | None = None
    review_count: int = 0
    submitted: bool = False
    last_action: ActionKind | None = None
    last_outcome: Outcome = Outcome.UNKNOWN
    confidence: float = 1.0
    elapsed_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.attempt < 0 or self.step < 0:
            raise DecisionError("trajectory counters must be >= 0")
        for value in (
            self.fields_total,
            self.required_total,
            self.required_missing,
            self.validation_errors,
            self.review_count,
        ):
            if value < 0:
                raise DecisionError("trajectory counters must be >= 0")
        if not 0.0 <= self.confidence <= 1.0:
            raise DecisionError("trajectory confidence must be within [0, 1]")
        if self.elapsed_seconds < 0:
            raise DecisionError("elapsed_seconds must be >= 0")
        if self.job_age_days is not None and self.job_age_days < 0:
            raise DecisionError("job_age_days must be >= 0")
        if not self.state_hash:
            raise DecisionError("trajectory state_hash is required")

    @property
    def progress(self) -> bool:
        return self.required_missing == 0 and self.validation_errors == 0 and self.review_count == 0

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt": self.attempt,
            "step": self.step,
            "state_hash": self.state_hash,
            "page_kind": self.page_kind.value,
            "fields_total": self.fields_total,
            "required_total": self.required_total,
            "required_missing": self.required_missing,
            "validation_errors": self.validation_errors,
            "captcha": self.captcha,
            "two_factor": self.two_factor,
            "account_wall": self.account_wall,
            "risk_allowed": self.risk_allowed,
            "quality_ready": self.quality_ready,
            "job_age_days": self.job_age_days,
            "review_count": self.review_count,
            "submitted": self.submitted,
            "last_action": self.last_action.value if self.last_action else None,
            "last_outcome": self.last_outcome.value,
            "confidence": self.confidence,
            "elapsed_seconds": self.elapsed_seconds,
        }


@dataclass(frozen=True)
class TrajectoryDecision:
    action: TrajectoryAction
    reason: str
    rule: str
    evidence: tuple[str, ...] = ()
    requires_human: bool = False

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise DecisionError("trajectory reason is required")
        if not self.rule.strip():
            raise DecisionError("trajectory rule is required")

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "rule": self.rule,
            "evidence": list(self.evidence),
            "requires_human": self.requires_human,
        }


def _decision(
    action: ActionKind,
    *,
    rule: str,
    rationale: str,
    target: str = "",
    value: str = "",
    value_source: ValueSource = ValueSource.NONE,
    confidence: float = 1.0,
    requires_human: bool = False,
    evidence: Iterable[str] = (),
) -> Decision:
    return Decision(
        action=action,
        target=target,
        value=value,
        value_source=value_source,
        confidence=confidence,
        rationale=rationale,
        policy_rule=rule,
        requires_human=requires_human,
        evidence=tuple(evidence),
    )


def _first_fillable(observation: Observation) -> FieldObservation | None:
    """Prefer required fields, then optional fields, in DOM order."""
    required = [item for item in observation.fillable if item.required]
    return (required or list(observation.fillable) or [None])[0]


def decide(observation: Observation) -> Decision:
    """Return one action using the ordered, first-match policy.

    Rules are intentionally ordered. Safety and terminal states come first,
    then review gaps, then filling, then navigation, then submit. The final
    fallback abstains rather than guessing.
    """
    if observation.captcha or observation.page_kind is PageKind.CAPTCHA:
        return _decision(
            ActionKind.STOP_CAPTCHA,
            rule="captcha",
            rationale="Captcha detected. JobForge never solves or bypasses it.",
            confidence=1.0,
            requires_human=True,
            evidence=("captcha=true",),
        )
    if observation.two_factor:
        return _decision(
            ActionKind.STOP_2FA,
            rule="two_factor",
            rationale="Two-factor challenge detected. Hand control to the human.",
            confidence=1.0,
            requires_human=True,
            evidence=("two_factor=true",),
        )
    if observation.account_wall or observation.page_kind is PageKind.ACCOUNT_WALL:
        return _decision(
            ActionKind.STOP_ACCOUNT_WALL,
            rule="account_wall",
            rationale="Employer account creation is out of scope. Hand control to the human.",
            confidence=1.0,
            requires_human=True,
            evidence=("account_wall=true",),
        )
    if observation.page_kind is PageKind.LOGIN:
        return _decision(
            ActionKind.STOP_ACCOUNT_WALL,
            rule="login_wall",
            rationale="Login or account wall detected. JobForge does not create or manage employer accounts.",
            confidence=1.0,
            requires_human=True,
            evidence=("page_kind=login",),
        )
    if not observation.risk_allowed:
        return _decision(
            ActionKind.STOP_RISK_GATE,
            rule="risk_gate",
            rationale="Risk consent or the daily submission cap is not satisfied.",
            confidence=1.0,
            evidence=("risk_allowed=false",),
        )
    if not observation.quality_ready:
        return _decision(
            ActionKind.STOP_QUALITY_GATE,
            rule="quality_gate",
            rationale="The quality gate is not ready. The browser must not proceed.",
            confidence=1.0,
            evidence=("quality_ready=false",),
        )
    if observation.page_kind is PageKind.UNSUPPORTED:
        return _decision(
            ActionKind.STOP_UNSUPPORTED,
            rule="unsupported_page",
            rationale="This page is not an allowlisted application flow.",
            confidence=1.0,
            evidence=("page_kind=unsupported",),
        )
    if observation.page_kind is PageKind.CLOSED:
        return _decision(
            ActionKind.STOP_CLOSED,
            rule="closed_posting",
            rationale="The posting is closed or no longer accepting applications.",
            confidence=1.0,
            evidence=("page_kind=closed",),
        )
    if (
        observation.job_age_days is not None
        and observation.job_age_days > MAX_JOB_AGE_DAYS
    ):
        return _decision(
            ActionKind.STOP_STALE,
            rule="stale_posting",
            rationale=(
                f"The posting is {observation.job_age_days} days old; the "
                f"freshness limit is {MAX_JOB_AGE_DAYS} days."
            ),
            confidence=1.0,
            evidence=(f"job_age_days={observation.job_age_days}",),
        )
    if observation.page_kind is PageKind.SUCCESS:
        return _decision(
            ActionKind.DONE,
            rule="success_page",
            rationale="The application reached a confirmation page.",
            confidence=1.0,
            evidence=("page_kind=success",),
        )
    if observation.page_kind is PageKind.UNKNOWN:
        return _decision(
            ActionKind.ABSTAIN,
            rule="unknown_page",
            rationale="Page state is unknown. Do not click or fill anything.",
            confidence=observation.confidence,
            requires_human=True,
            evidence=("page_kind=unknown",),
        )
    if not observation.fields and not observation.buttons:
        return _decision(
            ActionKind.ABSTAIN,
            rule="empty_observation",
            rationale="No actionable fields or buttons were observed.",
            confidence=min(observation.confidence, 0.5),
            requires_human=True,
        )

    # A repeated validation failure is not a filling problem; a human or a
    # different selector strategy is needed.
    if observation.validation_errors and observation.attempt >= 2:
        return _decision(
            ActionKind.ASK_HUMAN,
            rule="stuck_validation",
            rationale="Validation errors survived repeated attempts.",
            confidence=0.9,
            requires_human=True,
            evidence=observation.validation_errors,
        )

    fillable = _first_fillable(observation)
    if fillable is not None:
        return _decision(
            fillable.answer_action,
            rule="grounded_fill",
            target=fillable.field_id,
            value=fillable.answer_value,
            value_source=fillable.value_source,
            confidence=0.98,
            rationale=(
                f"Fill '{fillable.label}' from {fillable.value_source.value}; "
                "the value is already verified or human-approved."
            ),
            evidence=(f"category={fillable.category.value}", f"required={fillable.required}"),
        )

    review_fields = observation.required_review
    if review_fields:
        field_item = review_fields[0]
        return _decision(
            ActionKind.REQUEST_REVIEW,
            rule="required_review",
            target=field_item.field_id,
            confidence=0.95,
            requires_human=True,
            rationale=(
                f"Required field '{field_item.label}' has no grounded answer "
                f"({field_item.category.value}); do not guess."
            ),
            evidence=(f"category={field_item.category.value}", "sensitive" if field_item.is_sensitive else "review"),
        )

    submit = observation.submit_button
    if submit is not None and observation.complete:
        decision = _decision(
            ActionKind.SUBMIT,
            rule="submit_ready",
            target=submit.button_id,
            confidence=0.99,
            rationale="All required fields are resolved and both gates are ready.",
            evidence=("required_missing=0", "validation_errors=0", "quality_ready=true", "risk_allowed=true"),
        )
        decision.validate_against(observation)
        return decision

    next_button = observation.next_button
    if next_button is not None:
        return _decision(
            ActionKind.CLICK_NEXT,
            rule="next_step",
            target=next_button.button_id,
            confidence=0.95,
            rationale="The current step has no unresolved fill or review item.",
        )

    if observation.submit_button is not None:
        # The submit button exists but the form is not complete. The earlier
        # rules should have produced a review/fill action; if not, the state is
        # inconsistent and must not be guessed.
        return _decision(
            ActionKind.ABSTAIN,
            rule="submit_incomplete",
            rationale="Submit is present but the form is not complete. Do not click it.",
            confidence=0.8,
            requires_human=True,
            evidence=(f"required_missing={len(observation.required_missing)}",),
        )

    if observation.back_button is not None:
        return _decision(
            ActionKind.ABSTAIN,
            rule="no_forward_action",
            rationale="Only a back action is available; do not navigate blindly.",
            confidence=0.6,
            requires_human=True,
        )

    return _decision(
        ActionKind.ABSTAIN,
        rule="no_safe_action",
        rationale="No safe allowlisted action matches this observation.",
        confidence=min(observation.confidence, 0.5),
        requires_human=True,
    )


def _has_repeated_hash(history: list[TrajectoryState]) -> bool:
    if len(history) < 2:
        return False
    current = history[-1].state_hash
    return any(item.state_hash == current for item in history[:-1])


def _trailing_no_progress(history: list[TrajectoryState]) -> int:
    count = 0
    for item in reversed(history):
        if item.progress and not item.submitted:
            break
        count += 1
    return count


def _confidence_declining(history: list[TrajectoryState]) -> bool:
    if len(history) < 3:
        return False
    recent = [item.confidence for item in history[-3:]]
    return recent[0] > recent[1] > recent[2]


def evaluate_trajectory(
    history: list[TrajectoryState] | tuple[TrajectoryState, ...],
    *,
    max_steps: int = 40,
    max_seconds: float = 900.0,
) -> TrajectoryDecision:
    """Evaluate ordered recovery rules over the application trajectory.

    This mirrors Splice's trajectory monitor: report a typed action, never
    mutate the run. The caller decides whether to continue, roll back, or stop.
    """
    states = list(history)
    if not states:
        return TrajectoryDecision(
            action=TrajectoryAction.CONTINUE,
            rule="empty_history",
            reason="No trajectory history to evaluate.",
        )
    current = states[-1]
    if current.captcha:
        return TrajectoryDecision(
            action=TrajectoryAction.STOP_CAPTCHA,
            rule="captcha",
            reason="Captcha detected in the trajectory.",
            evidence=(f"step={current.step}",),
            requires_human=True,
        )
    if current.two_factor:
        return TrajectoryDecision(
            action=TrajectoryAction.STOP_2FA,
            rule="two_factor",
            reason="Two-factor challenge detected in the trajectory.",
            evidence=(f"step={current.step}",),
            requires_human=True,
        )
    if current.account_wall:
        return TrajectoryDecision(
            action=TrajectoryAction.STOP_ACCOUNT_WALL,
            rule="account_wall",
            reason="Account wall detected in the trajectory.",
            evidence=(f"step={current.step}",),
            requires_human=True,
        )
    if not current.risk_allowed:
        return TrajectoryDecision(
            action=TrajectoryAction.STOP_RISK,
            rule="risk_gate",
            reason="Risk gate is not satisfied.",
            evidence=(f"step={current.step}",),
        )
    if current.job_age_days is not None and current.job_age_days > MAX_JOB_AGE_DAYS:
        return TrajectoryDecision(
            action=TrajectoryAction.STOP_STALE,
            rule="stale_posting",
            reason=(
                f"The posting is {current.job_age_days} days old; the freshness "
                f"limit is {MAX_JOB_AGE_DAYS} days."
            ),
            evidence=(f"job_age_days={current.job_age_days}",),
        )
    if current.submitted or current.page_kind is PageKind.SUCCESS:
        return TrajectoryDecision(
            action=TrajectoryAction.DONE,
            rule="submitted",
            reason="The application reached submission or a confirmation page.",
            evidence=(f"step={current.step}",),
        )
    if current.step >= max_steps:
        return TrajectoryDecision(
            action=TrajectoryAction.STOP_LIMIT,
            rule="step_limit",
            reason=f"Step limit reached ({max_steps}).",
            evidence=(f"step={current.step}", f"max_steps={max_steps}"),
        )
    if current.elapsed_seconds >= max_seconds:
        return TrajectoryDecision(
            action=TrajectoryAction.STOP_LIMIT,
            rule="time_limit",
            reason=f"Wall-clock limit reached ({max_seconds:.0f}s).",
            evidence=(f"elapsed_seconds={current.elapsed_seconds:.1f}",),
        )
    if _has_repeated_hash(states):
        if len(states) >= 4 and states[-1].state_hash == states[-3].state_hash and states[-2].state_hash == states[-4].state_hash:
            return TrajectoryDecision(
                action=TrajectoryAction.STOP_STUCK,
                rule="oscillation",
                reason="The page state is oscillating between two known states.",
                evidence=tuple(item.state_hash for item in states[-4:]),
            )
        return TrajectoryDecision(
            action=TrajectoryAction.ASK_HUMAN,
            rule="repeated_state",
            reason="The current page state was seen before; a retry is unlikely to help.",
            evidence=(f"state_hash={current.state_hash}",),
            requires_human=True,
        )
    no_progress = _trailing_no_progress(states)
    if no_progress >= 3:
        return TrajectoryDecision(
            action=TrajectoryAction.STOP_STUCK,
            rule="no_progress",
            reason="Three consecutive states produced no required-field or validation progress.",
            evidence=(f"no_progress={no_progress}",),
        )
    if current.validation_errors >= 3:
        return TrajectoryDecision(
            action=TrajectoryAction.ASK_HUMAN,
            rule="validation_loop",
            reason="Validation errors have persisted across the last three states.",
            evidence=(f"validation_errors={current.validation_errors}",),
            requires_human=True,
        )
    if _confidence_declining(states):
        return TrajectoryDecision(
            action=TrajectoryAction.ASK_HUMAN,
            rule="confidence_decline",
            reason="Confidence declined across the last three states.",
            evidence=tuple(f"{item.confidence:.2f}" for item in states[-3:]),
            requires_human=True,
        )
    return TrajectoryDecision(
        action=TrajectoryAction.CONTINUE,
        rule="continue",
        reason="Trajectory remains within safe bounds.",
        evidence=(f"state_hash={current.state_hash}",),
    )
