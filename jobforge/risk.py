"""Opt-in risk policy with explicit disclosure.

Every risky mode is **off by default**. This project implements two bounded
submission modes: single-application unattended submit and sequential autopilot
submit from a queue. Both share one 10/day cap. Captcha solving, mass/parallel
submission, Indeed/LinkedIn automation, and employer account creation remain
deliberately unavailable; they are listed here so the UI and CLI can disclose
that clearly instead of pretending they exist.
"""

from __future__ import annotations

try:  # POSIX file locking; Windows uses msvcrt below.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

CONSENT_PHRASE = "I UNDERSTAND THE RISK"
POLICY_FILE = "risk.json"
USAGE_FILE = "risk_usage.json"

AVAILABLE_FLAGS = {"unattended_submit", "autopilot_submit"}
UNAVAILABLE_FLAGS = {
    "bulk_unattended",
    "captcha_solver",
    "indeed_linkedin_automation",
    "workday_account_creation",
}

WARNINGS = {
    "unattended_submit": (
        "UNATTENDED SUBMIT IS OFF BY DEFAULT.\n"
        "Enabling it lets JobForge click Submit without a human reviewing the form.\n"
        "Consequences you accept: applications you have not checked can go out, "
        "employer/ATS terms may be violated, and your accounts may be restricted.\n"
        "Captcha is never solved; a captcha stops the application and hands it back to you.\n"
        "Hard limit: {cap} unattended submissions per calendar day."
    ),
    "autopilot_submit": (
        "AUTOPILOT SUBMIT IS OFF BY DEFAULT.\n"
        "Enabling it lets the scheduled queue prepare and submit ready applications "
        "without a human review of each form.\n"
        "Consequences you accept: unchecked applications can go out, employer/ATS "
        "terms may be violated, and your accounts may be restricted.\n"
        "Autopilot is sequential, uses an ATS allowlist and the quality gate, and "
        "stops at captcha, 2FA, or an account wall.\n"
        "Hard limit: {cap} total unattended + autopilot submissions per calendar day."
    ),
    "bulk_unattended": (
        "MASS/PARALLEL UNATTENDED SUBMISSION IS NOT AVAILABLE. Autopilot submits "
        "sequentially and shares a hard 10/day cap with single-application submit."
    ),
    "captcha_solver": (
        "CAPTCHA SOLVING IS NOT AVAILABLE. JobForge never solves, farms, or bypasses "
        "captchas. Solve the challenge yourself in the browser."
    ),
    "indeed_linkedin_automation": (
        "INDEED/LINKEDIN AUTOMATION IS NOT AVAILABLE. Those platforms treat automation "
        "as a terms violation and can restrict your account. Apply manually."
    ),
    "workday_account_creation": (
        "EMPLOYER ACCOUNT CREATION IS NOT AVAILABLE. JobForge never creates employer "
        "accounts or stores employer passwords. Sign in yourself."
    ),
}


class RiskError(RuntimeError):
    """Raised when a risky action is attempted without consent or over the cap."""


@dataclass
class RiskPolicy:
    unattended_submit: bool = False
    autopilot_submit: bool = False
    daily_submit_cap: int = 10
    consented_at: str = ""
    consent_phrase: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def policy_path(config: Any) -> Path:
    return Path(config.data_dir) / POLICY_FILE


def usage_path(config: Any) -> Path:
    return Path(config.data_dir) / USAGE_FILE


def _lock_fd(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return
    if os.name == "nt":  # pragma: no cover - Windows is a separate gate
        import msvcrt

        if os.fstat(fd).st_size == 0:
            os.write(fd, b"1")
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        return
    raise RuntimeError("no reviewed file lock available for risk usage")


def _unlock_fd(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if os.name == "nt":  # pragma: no cover - Windows is a separate gate
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return


@contextmanager
def _usage_lock(config: Any) -> Iterator[None]:
    """Serialize the daily-cap check and increment across processes."""
    path = Path(config.data_dir) / "risk_usage.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _lock_fd(fd)
        yield
    finally:
        _unlock_fd(fd)
        os.close(fd)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


def load_policy(config: Any) -> RiskPolicy:
    path = policy_path(config)
    if not path.exists():
        return RiskPolicy()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RiskPolicy()
    if not isinstance(data, dict):
        return RiskPolicy()

    try:
        cap = int(data.get("daily_submit_cap", 10) or 10)
    except (TypeError, ValueError):
        cap = 10
    policy = RiskPolicy(
        unattended_submit=data.get("unattended_submit") is True,
        autopilot_submit=data.get("autopilot_submit") is True,
        daily_submit_cap=cap,
        consented_at=str(data.get("consented_at", "")),
        consent_phrase=str(data.get("consent_phrase", "")),
    )
    policy.daily_submit_cap = max(1, min(10, policy.daily_submit_cap))
    # A hand-edited boolean is not consent.
    if not policy.consented_at or policy.consent_phrase.strip() != CONSENT_PHRASE:
        policy.unattended_submit = False
        policy.autopilot_submit = False
    return policy


def save_policy(config: Any, policy: RiskPolicy) -> None:
    _atomic_json(policy_path(config), policy.to_dict())


def warning_text(flag: str, cap: int = 10) -> str:
    template = WARNINGS.get(flag, "UNKNOWN RISK FLAG")
    return template.format(cap=cap)


def enable(config: Any, flag: str, acknowledge: str, *, conn: Any | None = None) -> RiskPolicy:
    if flag in UNAVAILABLE_FLAGS:
        raise RiskError(warning_text(flag))
    if flag not in AVAILABLE_FLAGS:
        raise RiskError(f"Unknown risk flag '{flag}'. Available: {', '.join(sorted(AVAILABLE_FLAGS))}")
    if acknowledge.strip() != CONSENT_PHRASE:
        raise RiskError(f'Refusing to enable {flag}: type the exact phrase "{CONSENT_PHRASE}".')

    policy = load_policy(config)
    setattr(policy, flag, True)
    policy.consented_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    policy.consent_phrase = CONSENT_PHRASE
    save_policy(config, policy)
    _log_consent(conn, f"enabled {flag} cap={policy.daily_submit_cap}")
    return policy


def disable(config: Any, flag: str, *, conn: Any | None = None) -> RiskPolicy:
    policy = load_policy(config)
    if hasattr(policy, flag):
        setattr(policy, flag, False)
    if not (policy.unattended_submit or policy.autopilot_submit):
        policy.consent_phrase = ""
        policy.consented_at = ""
    save_policy(config, policy)
    _log_consent(conn, f"disabled {flag}")
    return policy


def _log_consent(conn: Any | None, detail: str) -> None:
    """Record consent changes in the local event log when a DB is available."""
    if conn is None:
        return
    from . import db

    db.log_event(conn, None, "risk_consent", detail)


def load_usage(config: Any) -> dict[str, Any]:
    path = usage_path(config)
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"date": today, "count": 0}
    if not isinstance(data, dict) or data.get("date") != today:
        return {"date": today, "count": 0}
    try:
        count = max(0, int(data.get("count", 0)))
    except (TypeError, ValueError):
        count = 0
    return {"date": today, "count": count}


def record_submission(config: Any) -> dict[str, Any]:
    with _usage_lock(config):
        usage = load_usage(config)
        usage["count"] = int(usage.get("count", 0)) + 1
        _atomic_json(usage_path(config), usage)
        return usage


def _check_submit(config: Any, flag: str) -> tuple[bool, str, RiskPolicy]:
    policy = load_policy(config)
    if not bool(getattr(policy, flag, False)):
        return (
            False,
            f"{flag.replace('_', ' ').capitalize()} is disabled. Enable it with: "
            f'jobforge risk enable --flag {flag} --acknowledge "{CONSENT_PHRASE}"',
            policy,
        )
    usage = load_usage(config)
    if int(usage.get("count", 0)) >= policy.daily_submit_cap:
        return (
            False,
            f"Daily submit cap reached ({policy.daily_submit_cap}) across unattended "
            "and autopilot modes. Wait until tomorrow or submit manually.",
            policy,
        )
    return True, "", policy


def _check_unattended(config: Any) -> tuple[bool, str, RiskPolicy]:
    return _check_submit(config, "unattended_submit")


def _check_autopilot(config: Any) -> tuple[bool, str, RiskPolicy]:
    return _check_submit(config, "autopilot_submit")


def can_submit_unattended(config: Any) -> tuple[bool, str, RiskPolicy]:
    """Read-only pre-check. Does not reserve a daily slot."""
    with _usage_lock(config):
        return _check_unattended(config)


def reserve_submission(config: Any) -> tuple[bool, str, RiskPolicy]:
    """Atomically check the policy and reserve one unattended-submit slot."""
    return _reserve_submit(config, "unattended_submit")


def can_submit_autopilot(config: Any) -> tuple[bool, str, RiskPolicy]:
    """Read-only pre-check for autopilot. Does not reserve a daily slot."""
    with _usage_lock(config):
        return _check_autopilot(config)


def reserve_autopilot_submission(config: Any) -> tuple[bool, str, RiskPolicy]:
    """Atomically reserve one slot for a sequential autopilot submission."""
    return _reserve_submit(config, "autopilot_submit")


def _reserve_submit(config: Any, flag: str) -> tuple[bool, str, RiskPolicy]:
    """Atomically check the policy and reserve one shared submission slot.

    The slot is consumed at attempt time, before the browser opens. A failed or
    captcha-blocked attempt still counts; under-counting would be unsafe.
    """
    with _usage_lock(config):
        allowed, reason, policy = _check_submit(config, flag)
        if not allowed:
            return allowed, reason, policy
        usage = load_usage(config)
        usage["count"] = int(usage.get("count", 0)) + 1
        _atomic_json(usage_path(config), usage)
        return True, "", policy


def status(config: Any) -> dict[str, Any]:
    policy = load_policy(config)
    usage = load_usage(config)
    return {
        "policy": policy.to_dict(),
        "usage": usage,
        "available": sorted(AVAILABLE_FLAGS),
        "unavailable": sorted(UNAVAILABLE_FLAGS),
        "warnings": {
            flag: warning_text(flag, policy.daily_submit_cap)
            for flag in sorted(AVAILABLE_FLAGS | UNAVAILABLE_FLAGS)
        },
        "consent_phrase": CONSENT_PHRASE,
    }
