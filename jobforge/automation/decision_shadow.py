"""Private shadow recording for the decision contract.

The decision layer is diagnostic in phase 1. It observes a page, records what
the policy would do, and changes nothing. Values are redacted before they reach
disk.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .decision_model import Decision, Observation

SHADOW_FILE = "decision_shadow.jsonl"
ENV_MODE = "JOBFORGE_DECISION_MODEL"
VALID_MODES = frozenset({"off", "shadow"})


def decision_mode() -> str:
    """Return the configured mode. ``active`` is not implemented yet."""
    raw = os.environ.get(ENV_MODE, "off").strip().lower()
    return raw if raw in VALID_MODES else "off"


def shadow_enabled() -> bool:
    return decision_mode() == "shadow"


def shadow_path(config: Any) -> Path:
    return Path(config.data_dir) / SHADOW_FILE


def record_shadow(
    path: str | Path,
    observation: Observation,
    decision: Decision,
    *,
    run_id: int | None = None,
    job_id: int | None = None,
    application_id: str = "",
    source: str = "live",
) -> Path:
    """Append one redacted decision record and return the path.

    The observation and decision serializers redact values by default. Do not
    pass ``include_values=True`` here.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "shadow",
        "source": source,
        "run_id": run_id,
        "job_id": job_id,
        "application_id": application_id,
        "observation": observation.to_dict(),
        "decision": decision.to_dict(),
    }
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
    finally:
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
    return target


def read_shadow(path: str | Path, *, limit: int = 100) -> list[dict[str, Any]]:
    """Read the most recent records without failing on a partial line."""
    if limit <= 0:
        return []
    target = Path(path)
    if not target.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for raw in lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records[-limit:]
