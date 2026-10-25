from __future__ import annotations

import json
import shutil

import pytest

from scripts.e2e_smoke import run_e2e


pytestmark = pytest.mark.skipif(shutil.which("typst") is None, reason="typst not installed")


def test_offline_e2e_smoke_reports_ok() -> None:
    verdict = run_e2e(timeout_s=30)

    assert verdict["ok"], json.dumps(verdict, indent=2)
    assert verdict["checks"], "expected at least one check"
    assert all(item["passed"] for item in verdict["checks"])
    assert verdict["port"] > 0
    assert verdict["pdf"].endswith("resume.pdf")
