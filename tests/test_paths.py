from __future__ import annotations

from pathlib import Path

from jobforge import paths
from jobforge.cli import build_parser
from jobforge.config import BrowserConfig, load_config


def test_app_home_honors_override(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    monkeypatch.setenv("JOBFORGE_HOME", str(home))
    assert paths.app_home() == home


def test_ensure_config_in_installed_mode_copies_example(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    monkeypatch.setattr(paths, "source_root", lambda: None)
    monkeypatch.setenv("JOBFORGE_HOME", str(home))

    config = paths.ensure_config()

    assert config == home / "config.yml"
    assert config.is_file()
    assert config.stat().st_mode & 0o777 == 0o600
    text = config.read_text(encoding="utf-8")
    assert "data_dir:" in text
    assert "browser:" in text


def test_relative_config_paths_resolve_to_app_home(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yml").write_text(
        "data_dir: data\noutput_dir: output\nbrowser:\n  user_data_dir: data/browser\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "source_root", lambda: None)
    monkeypatch.setenv("JOBFORGE_HOME", str(home))

    config = load_config()

    assert config.data_dir == home / "data"
    assert config.output_dir == home / "output"
    assert Path(config.browser.user_data_dir) == home / "data" / "browser"
    assert config.db_path == home / "data" / "jobforge.sqlite3"


def test_packaged_example_config_matches_checkout_copy():
    checkout = Path(__file__).resolve().parent.parent / "config.example.yml"
    packaged = paths.package_example_config()
    assert packaged.is_file()
    assert packaged.read_text(encoding="utf-8") == checkout.read_text(encoding="utf-8")


def test_bare_command_defaults_to_app():
    args = build_parser().parse_args([])
    assert args.command is None
    assert args.func.__name__ == "cmd_app"
    assert args.no_browser is False


def test_browser_channel_is_parsed(monkeypatch, tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.yml").write_text(
        "data_dir: data\noutput_dir: output\nbrowser:\n  channel: chrome\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(paths, "source_root", lambda: None)
    monkeypatch.setenv("JOBFORGE_HOME", str(home))

    assert load_config().browser.channel == "chrome"
    assert BrowserConfig().channel == ""
