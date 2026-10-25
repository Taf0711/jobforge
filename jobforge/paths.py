"""Filesystem locations for source checkouts and installed packages.

A source checkout keeps the historical layout: config, data, output, and
network files live next to ``pyproject.toml``. An installed wheel uses a
per-user app home so nothing is written into site-packages.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

APP_NAME = "JobForge"
PACKAGE_DIR = Path(__file__).resolve().parent


def source_root() -> Path | None:
    """Return the checkout root when running from a source tree."""
    root = PACKAGE_DIR.parent
    if (root / "pyproject.toml").is_file() or (root / ".git").is_dir():
        return root
    return None


def app_home() -> Path:
    """Per-user writable directory for config, data, output, and logs."""
    override = os.environ.get("JOBFORGE_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home())
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "jobforge"


def base_dir() -> Path:
    """Directory that relative config paths resolve against."""
    return source_root() or app_home()


def package_example_config() -> Path:
    return PACKAGE_DIR / "data" / "config.example.yml"


def example_config() -> Path:
    packaged = package_example_config()
    if packaged.is_file():
        return packaged
    checkout = source_root()
    if checkout is not None:
        return checkout / "config.example.yml"
    return packaged


def default_config_path() -> Path:
    override = os.environ.get("JOBFORGE_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    root = source_root()
    if root is not None:
        return root / "config.yml"
    return app_home() / "config.yml"


def ensure_config() -> Path:
    """Create the app home/config on first run and return the config path.

    In a source checkout this only creates the directory layout; it does not
    copy a config file, so the developer keeps using ``config.yml`` at the repo
    root.
    """
    root = source_root()
    if root is not None:
        return root / "config.yml"

    home = app_home()
    home.mkdir(parents=True, exist_ok=True)
    config = home / "config.yml"
    if not config.exists():
        packaged = example_config()
        if packaged.is_file():
            shutil.copyfile(packaged, config)
        else:
            config.write_text("", encoding="utf-8")
        try:
            config.chmod(0o600)
        except OSError:
            pass
    return config
