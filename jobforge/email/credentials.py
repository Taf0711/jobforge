"""Local secret storage for the IMAP password.

The password is never written to ``config.yml``, SQLite, exports, logs, or
templates. Lookup order is environment variable, OS keychain, then an atomic
0600 file under the JobForge data directory.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Any

KEYRING_SERVICE = "jobforge.email"


def _keyring_module() -> Any:
    try:
        import keyring  # type: ignore

        return keyring
    except Exception:  # noqa: BLE001 - optional dependency at runtime
        return None


def _usable_keyring() -> Any:
    keyring = _keyring_module()
    if keyring is None:
        return None
    try:
        backend = keyring.get_keyring()
        if "fail" in str(backend).lower():
            return None
    except Exception:  # noqa: BLE001
        return None
    return keyring


def _safe_username(username: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.@+-]+", "_", username.strip())
    return value[:120] or "default"


def _file_path(config: Any) -> Path | None:
    directory = str(getattr(config, "secret_dir", "") or "").strip()
    username = str(getattr(config, "username", "") or "").strip()
    if not directory or not username:
        return None
    return Path(directory).expanduser() / f"{_safe_username(username)}.key"


def load_password(config: Any) -> str:
    env_name = str(getattr(config, "password_env", "") or "").strip()
    if env_name:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    username = str(getattr(config, "username", "") or "").strip()
    if username:
        keyring = _usable_keyring()
        if keyring is not None:
            try:
                value = keyring.get_password(KEYRING_SERVICE, username)
                if value and str(value).strip():
                    return str(value).strip()
            except Exception:  # noqa: BLE001
                pass
    path = _file_path(config)
    if path is not None:
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except OSError:
            pass
    return ""


def password_source(config: Any) -> str:
    env_name = str(getattr(config, "password_env", "") or "").strip()
    if env_name and os.environ.get(env_name, "").strip():
        return "env"
    username = str(getattr(config, "username", "") or "").strip()
    if username:
        keyring = _usable_keyring()
        if keyring is not None:
            try:
                if keyring.get_password(KEYRING_SERVICE, username):
                    return "keyring"
            except Exception:  # noqa: BLE001
                pass
    path = _file_path(config)
    if path is not None:
        try:
            if path.is_file() and path.read_text(encoding="utf-8").strip():
                return "file"
        except OSError:
            pass
    return ""


def save_password(config: Any, password: str) -> str:
    value = (password or "").strip()
    if not value:
        raise ValueError("Password is empty")
    username = str(getattr(config, "username", "") or "").strip()
    if not username:
        raise ValueError("Set the IMAP username before saving a password")
    keyring = _usable_keyring()
    if keyring is not None:
        try:
            keyring.set_password(KEYRING_SERVICE, username, value)
            path = _file_path(config)
            if path is not None:
                path.unlink(missing_ok=True)
            return "keyring"
        except Exception:  # noqa: BLE001 - fall through to 0600 file
            pass
    path = _file_path(config)
    if path is None:
        raise ValueError("No local secret directory is configured")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd, temp_name = tempfile.mkstemp(prefix=".email-", dir=str(path.parent), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return "file"


def delete_password(config: Any) -> None:
    username = str(getattr(config, "username", "") or "").strip()
    if username:
        keyring = _usable_keyring()
        if keyring is not None:
            try:
                keyring.delete_password(KEYRING_SERVICE, username)
            except Exception:  # noqa: BLE001
                pass
    path = _file_path(config)
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
