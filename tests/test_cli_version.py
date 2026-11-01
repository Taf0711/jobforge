from __future__ import annotations

import pytest

from jobforge.cli import _package_version, build_parser


def test_version_flag_prints_package_version(capsys):
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])

    assert excinfo.value.code == 0
    assert _package_version() in capsys.readouterr().out
