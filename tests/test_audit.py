from __future__ import annotations

import pytest

from jobforge.audit.security import find_danger, run_security


def test_security_audit_has_no_high_findings():
    result = run_security()
    assert result["high"] == 0, result["findings"]


def test_security_audit_has_no_medium_findings():
    result = run_security()
    assert result["medium"] == 0, result["findings"]


def test_danger_patterns_catch_dict_and_assignment_forms():
    assert find_danger('kwargs["allow_submit"] = True')[0][1] == "auto-submit enabled"
    assert find_danger("allow_submit = True")[0][1] == "auto-submit enabled"
    assert find_danger("captcha_solver = True")[0][1] == "captcha-solving reference"
    assert find_danger('{"captcha_solver": true}')[0][1] == "captcha-solving reference"
    assert find_danger("captcha.solve(page)")[0][1] == "captcha-solving reference"


def test_danger_patterns_do_not_flag_safe_values():
    assert find_danger("allow_submit = False") == []
    assert find_danger("captcha_solver = False") == []
    assert find_danger("captcha_solver = None") == []


def test_reviewed_risk_gated_call_site_can_be_marked():
    source = 'kwargs["allow_submit"] = True  # security-audit: allow auto-submit enabled'
    assert find_danger(source) == []


def test_marker_only_suppresses_the_named_finding():
    source = 'os.system(cmd); kwargs["allow_submit"] = True  # security-audit: allow auto-submit enabled'
    assert find_danger(source) == [(1, "os.system()")]
    assert find_danger('eval(user_input)  # security-audit: allow auto-submit enabled') == [(1, "eval()")]


def test_marker_must_follow_the_match_and_name_the_finding():
    assert find_danger('note = "# security-audit: allow auto-submit enabled"; allow_submit = True')
    assert find_danger('kwargs["allow_submit"] = True  # security-audit: allow eval()')
    assert find_danger('kwargs["allow_submit"] = True  # security-audit: allow')


def test_ssrf_guard_rejects_localhost():
    from jobforge.sources.base import SourceError, assert_public_url

    with pytest.raises(SourceError):
        assert_public_url("http://127.0.0.1:8080/")
    with pytest.raises(SourceError):
        assert_public_url("file:///etc/passwd")
