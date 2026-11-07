"""Single source for the local app version shown by the dashboard and CLI."""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version

from .paths import source_root


def app_version() -> str:
    root = source_root()
    if root is not None:
        try:
            with (root / "pyproject.toml").open("rb") as handle:
                return str(tomllib.load(handle)["project"]["version"])
        except Exception:  # noqa: BLE001 - fall back to installed metadata
            pass
    try:
        return version("jobforge")
    except PackageNotFoundError:
        return "dev"
