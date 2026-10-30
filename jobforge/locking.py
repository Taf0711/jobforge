"""Cross-process domain-action lock.

JobForge has several writers that can conflict: SQLite, the JSON queue, the
persistent browser profile, resume artifacts, and the dashboard. The initial
release uses one lock per canonical data directory. Reads do not acquire it;
writers do. The lock is reentrant inside a process so an autopilot pass can call
a service that also locks the same data directory.

POSIX uses ``fcntl.flock``. Windows uses ``msvcrt.locking`` over one byte.
Other platforms fail closed with ``ActionLockUnavailable`` rather than silently
running without serialization.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

ACTION_LOCK_FILE = "action.lock"


class ActionBusy(RuntimeError):
    """Another process or thread holds the domain-action lock."""


class ActionLockUnavailable(RuntimeError):
    """The platform cannot provide a reviewed file lock."""


class _LockState:
    __slots__ = ("rlock", "depth", "fd")

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.depth = 0
        self.fd: int | None = None


_STATES: dict[str, _LockState] = {}
_STATES_GUARD = threading.Lock()
_F = TypeVar("_F", bound=Callable[..., Any])


def _state_for(path: Path) -> _LockState:
    key = str(path)
    with _STATES_GUARD:
        state = _STATES.get(key)
        if state is None:
            state = _LockState()
            _STATES[key] = state
        return state


def lock_path(target: Any) -> Path:
    """Resolve a config, data directory, or explicit path to the lock file."""
    base = getattr(target, "data_dir", None)
    root = Path(base) if base is not None else Path(target)
    if root.suffix == ".lock":
        return root
    return root / ACTION_LOCK_FILE


def _acquire_fd(fd: int, *, blocking: bool) -> bool:
    if os.name == "posix":
        import fcntl

        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
            return True
        except BlockingIOError:
            return False
    if os.name == "nt":  # pragma: no cover - Windows is a separate gate
        import msvcrt

        try:
            # Lock one byte from the current position. The file is created with
            # a seed byte so a lock is always inside the file.
            msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    raise ActionLockUnavailable(f"no reviewed file lock for os.name={os.name!r}")


def _release_fd(fd: int) -> None:
    if os.name == "posix":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
    elif os.name == "nt":  # pragma: no cover - Windows is a separate gate
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


@contextmanager
def action_lock_path(path: str | Path, *, blocking: bool = True) -> Iterator[None]:
    """Acquire the action lock; nested calls in one process are reentrant."""
    target = lock_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    state = _state_for(target)
    if not state.rlock.acquire(blocking=blocking):
        raise ActionBusy(f"domain action already running: {target}")
    try:
        if state.depth == 0:
            fd = os.open(target, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"1")
                    os.lseek(fd, 0, os.SEEK_SET)
                if not _acquire_fd(fd, blocking=blocking):
                    raise ActionBusy(f"domain action already running: {target}")
                state.fd = fd
            except Exception:
                os.close(fd)
                raise
        state.depth += 1
        try:
            yield
        finally:
            state.depth -= 1
            if state.depth == 0 and state.fd is not None:
                try:
                    _release_fd(state.fd)
                finally:
                    os.close(state.fd)
                    state.fd = None
    finally:
        state.rlock.release()


def action_lock(target: Any, *, blocking: bool = True) -> Iterator[None]:
    """Acquire the lock for a Config, data directory, or explicit lock path."""
    return action_lock_path(lock_path(target), blocking=blocking)


def locked_action(fn: _F) -> _F:
    """Decorate a service function whose first argument is a Config.

    The lock is reentrant, so an autopilot pass can call a locked service and
    the service can call another locked service without deadlocking.
    """

    @wraps(fn)
    def wrapper(config: Any, *args: Any, **kwargs: Any) -> Any:
        with action_lock(config):
            return fn(config, *args, **kwargs)

    return wrapper  # type: ignore[return-value]
