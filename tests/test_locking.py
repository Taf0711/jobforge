from __future__ import annotations

import threading
from pathlib import Path

import pytest

from jobforge.locking import ActionBusy, action_lock, action_lock_path, lock_path


def test_lock_path_accepts_config_like_and_directory(tmp_path: Path):
    class Config:
        data_dir = tmp_path / "data"

    assert lock_path(Config()) == tmp_path / "data" / "action.lock"
    assert lock_path(tmp_path) == tmp_path / "action.lock"
    assert lock_path(tmp_path / "custom.lock") == tmp_path / "custom.lock"


def test_action_lock_is_reentrant(tmp_path: Path):
    with action_lock_path(tmp_path):
        with action_lock_path(tmp_path):
            assert (tmp_path / "action.lock").exists()
    assert (tmp_path / "action.lock").stat().st_mode & 0o777 == 0o600


def test_action_lock_rejects_another_thread(tmp_path: Path):
    entered = threading.Event()
    release = threading.Event()
    errors: list[Exception] = []

    def holder() -> None:
        with action_lock_path(tmp_path):
            entered.set()
            release.wait(timeout=5)

    thread = threading.Thread(target=holder)
    thread.start()
    assert entered.wait(timeout=5)
    try:
        with pytest.raises(ActionBusy):
            with action_lock_path(tmp_path, blocking=False):
                pass
    finally:
        release.set()
        thread.join(timeout=5)
    assert errors == []


def _try_lock_in_child(path: str, queue) -> None:
    try:
        with action_lock_path(path, blocking=False):
            queue.put("acquired")
    except ActionBusy:
        queue.put("busy")


def test_action_lock_rejects_another_process(tmp_path: Path):
    import multiprocessing

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    with action_lock_path(tmp_path):
        process = ctx.Process(target=_try_lock_in_child, args=(str(tmp_path), queue))
        process.start()
        try:
            assert queue.get(timeout=10) == "busy"
        finally:
            process.join(timeout=10)
        assert process.exitcode == 0
