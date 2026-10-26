"""GitHub connection, repository context, and project import.

The connection is deliberately local-only:

- The token is sent only to ``api.github.com``.
- A token supplied in the dashboard is stored in the OS keychain when
  available, with a 0600 file fallback under the local data directory.
- ``GITHUB_TOKEN`` / ``JOBFORGE_GITHUB_TOKEN`` can be used without storing a
  copy, which is useful for CI and terminal-first users.
- Repository metadata and README excerpts stay in SQLite. Importing a project
  into the resume is an explicit, reviewed action; syncing repositories alone
  never changes the resume.

Raw GitHub API responses and README text are never sent to an LLM. If the user
imports approved bullets into the fact bank and later enables the optional
cover-letter LLM, those bullets are treated like any other profile fact. README
text is treated as untrusted display content and is never marked as trusted
HTML.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from .. import db
from ..matching.skills import extract_skills
from ..models import Profile

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
KEYRING_SERVICE = "jobforge.github"
KEYRING_USER = "default"
TOKEN_ENV_NAMES = ("JOBFORGE_GITHUB_TOKEN", "GITHUB_TOKEN")
TOKEN_FILE = "github.token"
README_LIMIT = 64 * 1024
RESPONSE_LIMIT = 2 * 1024 * 1024
MAX_REPOS = 300
MAX_PAGES = 3
USER_AGENT = "JobForge/0.4 (+https://github.com/Taf0711/jobforge)"


class GitHubError(RuntimeError):
    """A user-facing GitHub operation failed."""


class GitHubAuthError(GitHubError):
    """The token is missing, expired, revoked, or lacks access."""


class GitHubRateLimitError(GitHubError):
    """GitHub's API rate limit or abuse guard was hit."""


def _env_token() -> str:
    for name in TOKEN_ENV_NAMES:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _load_keyring():
    try:
        import keyring  # type: ignore

        return keyring
    except Exception:  # noqa: BLE001  # pragma: no cover - optional dependency
        return None


@dataclass
class TokenRecord:
    token: str
    backend: str
    warning: str = ""


class GitHubTokenStore:
    """Keychain-first token storage with an atomic 0600 fallback."""

    def __init__(self, data_dir: str | Path, *, keyring_module: Any = None) -> None:
        self.data_dir = Path(data_dir)
        self.token_path = self.data_dir / "connections" / TOKEN_FILE
        if keyring_module is False:
            self._keyring = None
        else:
            self._keyring = _load_keyring() if keyring_module is None else keyring_module
        self._keyring_error = ""
        if keyring_module is None and self._keyring is not None:
            try:
                backend = self._keyring.get_keyring()
                if "fail" in str(backend).lower():
                    self._keyring = None
                    self._keyring_error = "No usable OS keychain backend"
            except Exception as exc:  # noqa: BLE001  # pragma: no cover - platform dependent
                self._keyring = None
                self._keyring_error = str(exc)

    @property
    def keyring_available(self) -> bool:
        return self._keyring is not None

    @property
    def env_token(self) -> str:
        return _env_token()

    def load(self, backend: str = "") -> TokenRecord | None:
        """Load a token, optionally from one specific source.

        The connection row records the source chosen at connect time. Honoring
        that source prevents a stale keychain entry from overriding the
        environment token (or the reverse).
        """
        backend = (backend or "").strip()
        if backend in {"", "keyring"} and self._keyring is not None:
            try:
                token = self._keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
                if token:
                    return TokenRecord(token=str(token).strip(), backend="keyring")
            except Exception as exc:  # noqa: BLE001
                self._keyring_error = str(exc)
        if backend in {"", "file"}:
            try:
                if self.token_path.is_file():
                    token = self.token_path.read_text(encoding="utf-8").strip()
                    if token:
                        return TokenRecord(
                            token=token,
                            backend="file",
                            warning="Token is stored in a local 0600 file; install an OS keychain backend to encrypt it at rest.",
                        )
            except OSError:
                pass
        if backend in {"", "env"}:
            token = self.env_token
            if token:
                return TokenRecord(token=token, backend="env")
        return None

    def save(self, token: str) -> TokenRecord:
        token = _validated_token(token)
        if self._keyring is not None:
            try:
                self._keyring.set_password(KEYRING_SERVICE, KEYRING_USER, token)
                self._remove_file()
                return TokenRecord(token=token, backend="keyring")
            except Exception as exc:  # noqa: BLE001
                self._keyring_error = str(exc)
        self._write_file(token)
        return TokenRecord(
            token=token,
            backend="file",
            warning="OS keychain unavailable; token is stored in a local 0600 file.",
        )

    def delete(self) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(KEYRING_SERVICE, KEYRING_USER)
            except Exception as exc:  # noqa: BLE001
                self._keyring_error = str(exc)
        self._remove_file()

    def _write_file(self, token: str) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.token_path.parent, 0o700)
        except OSError:
            pass
        fd, temp_name = tempfile.mkstemp(
            prefix=".github-", dir=str(self.token_path.parent), text=True
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(token + "\n")
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, self.token_path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def _remove_file(self) -> None:
        try:
            self.token_path.unlink(missing_ok=True)
        except OSError:
            pass


def _connection_token(store: GitHubTokenStore, connection: dict[str, Any] | None) -> TokenRecord | None:
    """Honor the connection's token source instead of a stale keychain entry."""
    if connection and connection.get("token_backend"):
        return store.load(str(connection["token_backend"]))
    return store.load()


def _validated_token(token: str) -> str:
    token = (token or "").strip()
    if not token:
        raise GitHubAuthError("Enter a GitHub token.")
    if len(token) > 512 or any(char.isspace() for char in token):
        raise GitHubAuthError("That does not look like a GitHub token.")
    return token


def _read_capped(response: requests.Response, limit: int = RESPONSE_LIMIT) -> bytes:
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_content(chunk_size=16384):
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise GitHubError("GitHub response was larger than the local safety limit.")
        chunks.append(chunk)
    return b"".join(chunks)


class GitHubClient:
    """Small, restricted GitHub REST client.

    Only fixed API paths are supported. Token material never appears in URLs,
    error messages, or returned data.
    """

    def __init__(
        self,
        token: str,
        *,
        session: requests.Session | None = None,
        timeout: int = 20,
    ) -> None:
        self._token = _validated_token(token)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            }
        )

    def _request(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
        allow_404: bool = False,
        limit: int = RESPONSE_LIMIT,
    ) -> tuple[bytes | None, dict[str, str]]:
        url = API_ROOT + path
        try:
            response = self.session.get(
                url,
                params=params,
                headers={"Accept": accept},
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
        except requests.RequestException as exc:
            raise GitHubError(f"Could not reach GitHub: {exc}") from exc
        with response:
            headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
            if response.status_code == 404 and allow_404:
                return None, headers
            if response.status_code == 401:
                raise GitHubAuthError(
                    "GitHub rejected the token. Reconnect with a valid, unexpired token."
                )
            if response.status_code in {403, 429}:
                if headers.get("x-ratelimit-remaining") == "0" or response.status_code == 429:
                    reset = headers.get("x-ratelimit-reset", "")
                    hint = f" Resets at {int(reset)} (Unix time)." if reset.isdigit() else ""
                    raise GitHubRateLimitError(
                        "GitHub rate limit reached. Wait a few minutes before syncing again."
                        + hint
                    )
                raise GitHubAuthError(
                    "GitHub denied this request. Check that the token can read the repository."
                )
            if not 200 <= response.status_code < 300:
                raise GitHubError(
                    f"GitHub returned HTTP {response.status_code} for {path}."
                )
            return _read_capped(response, limit=limit), headers

    def _json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        body, _headers = self._request(path, params=params)
        try:
            return json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise GitHubError("GitHub returned malformed JSON.") from exc

    def user(self) -> dict[str, Any]:
        body, headers = self._request("/user")
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            raise GitHubError("GitHub returned malformed account data.") from exc
        if not isinstance(data, dict) or not data.get("login"):
            raise GitHubAuthError("GitHub did not return an account for this token.")
        return {
            "account_id": str(data.get("id") or ""),
            "login": str(data.get("login") or ""),
            "display_name": str(data.get("name") or ""),
            "avatar_url": str(data.get("avatar_url") or ""),
            "html_url": str(data.get("html_url") or ""),
            "scopes": headers.get("x-oauth-scopes", "").strip(),
            "public_repos": int(data.get("public_repos") or 0),
            "total_private_repos": int(data.get("total_private_repos") or 0),
            "plan": str((data.get("plan") or {}).get("name") or ""),
        }

    def repos(self, *, max_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
        repos: list[dict[str, Any]] = []
        for page in range(1, max_pages + 1):
            data = self._json(
                "/user/repos",
                params={
                    "affiliation": "owner",
                    "visibility": "all",
                    "sort": "pushed",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            if not isinstance(data, list) or not data:
                break
            repos.extend(normalize_repo(item) for item in data if isinstance(item, dict))
            if len(data) < 100 or len(repos) >= MAX_REPOS:
                break
        return repos[:MAX_REPOS]

    def repo_context(self, owner: str, name: str) -> tuple[dict[str, Any], str]:
        owner = _repo_part(owner)
        name = _repo_part(name)
        languages = self._json(f"/repos/{quote(owner)}/{quote(name)}/languages")
        if not isinstance(languages, dict):
            languages = {}
        body, _headers = self._request(
            f"/repos/{quote(owner)}/{quote(name)}/readme",
            accept="application/vnd.github.raw",
            allow_404=True,
            limit=README_LIMIT,
        )
        readme = (body or b"").decode("utf-8", errors="replace")
        return {str(k): int(v or 0) for k, v in languages.items()}, readme


def _repo_part(value: str) -> str:
    value = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value):
        raise GitHubError("Invalid GitHub repository name.")
    return value


def _tech_line(language: str, topics: Iterable[str], languages: dict[str, Any] | None = None) -> str:
    items: list[str] = []
    if language:
        items.append(str(language))
    # GitHub language bytes are more informative than the primary-language badge.
    ordered = sorted(
        ((str(name), int(size or 0)) for name, size in (languages or {}).items()),
        key=lambda item: (-item[1], item[0].lower()),
    )
    for name, _size in ordered[:3]:
        if name and name not in items:
            items.append(name)
    for topic in topics:
        topic = str(topic).strip().replace("-", " ")
        if topic and topic.lower() not in {item.lower() for item in items}:
            items.append(topic)
    return ", ".join(items[:10])


def _tags_for(*values: Any) -> list[str]:
    text = " ".join(str(value or "") for value in values)
    return list(dict.fromkeys(extract_skills(text)))[:20]


def normalize_repo(data: dict[str, Any]) -> dict[str, Any]:
    owner = str((data.get("owner") or {}).get("login") or "")
    topics = [str(item).strip() for item in (data.get("topics") or []) if str(item).strip()]
    language = str(data.get("language") or "")
    description = str(data.get("description") or "")
    return {
        "repo_id": int(data.get("id") or 0),
        "owner": owner,
        "full_name": str(data.get("full_name") or ""),
        "name": str(data.get("name") or ""),
        "html_url": str(data.get("html_url") or ""),
        "description": description,
        "homepage": str(data.get("homepage") or ""),
        "language": language,
        "languages": {},
        "topics": topics,
        "stars": int(data.get("stargazers_count") or 0),
        "forks": int(data.get("forks_count") or 0),
        "private": bool(data.get("private")),
        "fork": bool(data.get("fork")),
        "archived": bool(data.get("archived")),
        "pushed_at": str(data.get("pushed_at") or ""),
        "created_at": str(data.get("created_at") or ""),
        "readme": "",
        "tech_line": _tech_line(language, topics),
        "tags": _tags_for(language, description, " ".join(topics)),
        "suggested_bullets": [],
    }


README_SECTION = re.compile(
    r"^(?:what it (?:actually )?(?:does|watches)|what it is|features|highlights|overview|"
    r"about|how it works|key features|capabilities|key capabilities|what this does)\s*:?$",
    re.IGNORECASE,
)
EXCLUDED_SECTION = re.compile(
    r"^(?:install|installation|setup|getting started|usage|run|testing|tests|contributing|"
    r"license|requirements|prerequisites|configuration|deploy|deployment|changelog)\s*:?$",
    re.IGNORECASE,
)
EXCLUDED_LINE = re.compile(
    r"(?:npm install|pip install|pnpm install|yarn install|git clone|docker run|"
    r"docker compose|make install|brew install|apt install|python -m|"
    r"localhost:|https?://|www\.|license|badge|shields\.io|contributing)",
    re.IGNORECASE,
)
MARKDOWN_NOISE = re.compile(r"!\[[^\]]*\]\([^)]*\)|\[!\[[^\]]*\]\([^)]*\)\]\([^)]*\)")


def clean_readme(readme: str) -> str:
    text = MARKDOWN_NOISE.sub("", readme or "")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    # Only strip real HTML tags. A broad <[^>]+> also eats prose between a
    # literal "<" and a later ">" (for example ">Apply<" in a README).
    text = re.sub(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>", "", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    return text


def bullets_from_readme(readme: str, *, limit: int = 3) -> list[str]:
    """Extract a few reviewable project bullets from the owner's README.

    This is a suggestion engine, not a resume writer. The dashboard prefills a
    textarea; the human edits and explicitly imports the result.
    """
    text = clean_readme(readme)
    lines = text.splitlines()
    section = ""
    candidates: list[str] = []
    current: str | None = None
    current_section = ""
    in_code = False

    def flush() -> None:
        nonlocal current
        if current is None:
            return
        value = re.sub(r"\s+", " ", current).strip()
        value = re.sub(r"(\*\*|__)(.*?)\1", r"\2", value)
        value = re.sub(r"(?<!\w)\*([^*]+)\*(?!\w)", r"\1", value)
        value = value.strip(" -–—:;,.")
        current = None
        if not (README_SECTION.match(current_section) and not EXCLUDED_SECTION.match(current_section)):
            return
        if len(value) > 260:
            first_sentence = re.split(r"(?<=[.!?])\s+", value)[0]
            value = first_sentence if len(first_sentence) <= 260 else ""
        if len(value) < 24 or len(value) > 260:
            return
        if EXCLUDED_LINE.search(value):
            return
        if value.lower() not in {existing.lower() for existing in candidates}:
            candidates.append(value)

    for raw in lines:
        line = raw.strip()
        if line.startswith("```"):
            flush()
            in_code = not in_code
            continue
        if in_code:
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", line)
        if heading:
            flush()
            section = heading.group(1).strip()
            continue
        bullet = re.match(r"^(?:[-*+]|\d+[.)])\s+(.+)$", line)
        if bullet:
            flush()
            current = bullet.group(1)
            current_section = section
            continue
        if current is not None:
            if not line:
                flush()
            elif raw[:1].isspace() and not heading and not bullet:
                current += " " + line
            else:
                flush()
    flush()
    return candidates[:limit]


def project_from_repo(
    repo: dict[str, Any],
    *,
    title: str,
    tech_line: str,
    bullets: list[str],
    tags: list[str] | None = None,
) -> dict[str, Any]:
    repo_id = int(repo.get("repo_id") or 0)
    clean_title = _clean_text(title, 120) or _clean_text(repo.get("name", ""), 120)
    clean_tech = _clean_text(tech_line, 220)
    clean_bullets = [_clean_text(item, 500) for item in bullets if _clean_text(item, 500)][:5]
    clean_tags = list(dict.fromkeys(_clean_text(item, 60) for item in (tags or []) if _clean_text(item, 60)))[:20]
    return {
        "title": clean_title,
        "name": clean_title,
        "company": "",
        "description": _clean_text(repo.get("description", ""), 500),
        "tech_line": clean_tech,
        "tags": clean_tags,
        "best_for": [],
        "bullets": clean_bullets,
        "url": str(repo.get("html_url") or ""),
        "source": "github",
        "source_id": f"github-{repo_id}",
        "repo_id": repo_id,
        "repo_full_name": str(repo.get("full_name") or ""),
        "stars": int(repo.get("stars") or 0),
        "pushed_at": str(repo.get("pushed_at") or ""),
        "dead": False,
        "notes": "Imported from GitHub; edit before tailoring.",
    }


def _clean_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit].rstrip()


def find_repo_project(profile: Profile, repo_id: int) -> dict[str, Any] | None:
    source_id = f"github-{int(repo_id)}"
    for project in profile.projects:
        if str(project.get("source_id") or "") == source_id:
            return project
    return None


def imported_repo_ids(profile: Profile) -> set[int]:
    ids: set[int] = set()
    for project in profile.projects:
        if project.get("source") == "github" and project.get("repo_id"):
            try:
                ids.add(int(project["repo_id"]))
            except (TypeError, ValueError):
                continue
    return ids


def import_repo_project(
    profile: Profile,
    repo: dict[str, Any],
    *,
    title: str,
    tech_line: str,
    bullets: list[str],
    tags: list[str] | None = None,
    add_skills: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    """Add or update one reviewed repository in the active profile."""
    project = project_from_repo(
        repo,
        title=title,
        tech_line=tech_line,
        bullets=bullets,
        tags=list(repo.get("tags") or []) if tags is None else tags,
    )
    source_id = str(project["source_id"])
    repo_id = int(project["repo_id"])

    existing_index = next(
        (
            index
            for index, item in enumerate(profile.projects)
            if str(item.get("source_id") or "") == source_id
        ),
        None,
    )
    if existing_index is None:
        profile.projects.append(project)
    else:
        previous = profile.projects[existing_index]
        # Keep manually curated metadata that is not part of this form.
        for key in ("best_for", "dead"):
            if key in previous:
                project[key] = previous[key]
        profile.projects[existing_index] = project

    # Rebuild only this repository's project facts; do not touch resume-forge
    # or parsed-resume facts.
    profile.facts = [
        fact
        for fact in profile.facts
        if str(fact.get("source_id") or "") != source_id
    ]
    for index, bullet in enumerate(project["bullets"], start=1):
        profile.facts.append(
            {
                "id": f"gh-proj-{repo_id}-{index:02d}",
                "section": "projects",
                "title": project["title"],
                "company": "",
                "text": bullet,
                "skills": _tags_for(bullet, project["tech_line"], " ".join(project["tags"])),
                "source_id": source_id,
            }
        )

    added_skills: list[str] = []
    if add_skills:
        detected = _tags_for(
            project["title"],
            project["tech_line"],
            " ".join(project["tags"]),
            " ".join(project["bullets"]),
            repo.get("description", ""),
        )
        existing_skills = set(profile.skills)
        added_skills = [skill for skill in detected if skill not in existing_skills]
        profile.skills = list(dict.fromkeys([*profile.skills, *detected]))

    return project, added_skills


def remove_repo_project(profile: Profile, repo_id: int) -> bool:
    source_id = f"github-{int(repo_id)}"
    before = len(profile.projects)
    profile.projects = [
        project
        for project in profile.projects
        if str(project.get("source_id") or "") != source_id
    ]
    profile.facts = [
        fact
        for fact in profile.facts
        if str(fact.get("source_id") or "") != source_id
    ]
    return len(profile.projects) < before


def connect_github(
    store: GitHubTokenStore,
    conn: sqlite3.Connection,
    token: str | None = None,
) -> dict[str, Any]:
    """Validate and store a token, then sync repository metadata."""
    supplied = (token or "").strip()
    env = store.env_token
    raw_token = supplied or env
    if not raw_token:
        raise GitHubAuthError("Enter a GitHub token or set GITHUB_TOKEN.")

    # Validate against GitHub before persisting anything. An invalid token must
    # not be left behind in the keychain or the fallback file.
    client = GitHubClient(raw_token)
    account = client.user()
    repos = client.repos()
    record = store.save(supplied) if supplied else TokenRecord(token=env, backend="env")
    db.save_connection(
        conn,
        "github",
        account_id=account["account_id"],
        login=account["login"],
        display_name=account["display_name"],
        scopes=account["scopes"],
        token_backend=record.backend,
        meta=account,
    )
    count = db.upsert_github_repos(conn, repos)
    db.touch_connection_sync(conn, "github")
    db.log_event(conn, None, "github_connected", f"@{account['login']} ({count} repositories)")
    return {
        "account": account,
        "repositories": count,
        "token_backend": record.backend,
        "warning": record.warning,
    }


def sync_github(store: GitHubTokenStore, conn: sqlite3.Connection) -> dict[str, Any]:
    connection = db.get_connection(conn, "github")
    if not connection:
        raise GitHubAuthError("GitHub is not connected.")
    record = _connection_token(store, connection)
    if not record:
        raise GitHubAuthError("The stored GitHub token is missing. Reconnect GitHub.")
    client = GitHubClient(record.token)
    account = client.user()
    repos = client.repos()
    db.save_connection(
        conn,
        "github",
        account_id=account["account_id"],
        login=account["login"],
        display_name=account["display_name"],
        scopes=account["scopes"],
        token_backend=record.backend,
        meta=account,
    )
    count = db.upsert_github_repos(conn, repos)
    db.touch_connection_sync(conn, "github")
    db.log_event(conn, None, "github_synced", f"@{account['login']} ({count} repositories)")
    return {"account": account, "repositories": count, "token_backend": record.backend}


def disconnect_github(store: GitHubTokenStore, conn: sqlite3.Connection) -> None:
    connection = db.get_connection(conn, "github")
    login = (connection or {}).get("login", "")
    store.delete()
    db.delete_connection(conn, "github")
    # Repository cache contains private metadata and README excerpts. Remove it
    # on disconnect; projects the human explicitly imported stay in the profile.
    db.clear_github_repos(conn)
    db.log_event(conn, None, "github_disconnected", f"@{login}" if login else "")


def refresh_repo_context(
    store: GitHubTokenStore,
    conn: sqlite3.Connection,
    repo: dict[str, Any],
) -> dict[str, Any]:
    """Fetch languages + README for one repository and cache the suggestion."""
    connection = db.get_connection(conn, "github")
    record = _connection_token(store, connection)
    if not record:
        raise GitHubAuthError("The stored GitHub token is missing. Reconnect GitHub.")
    client = GitHubClient(record.token)
    languages, readme = client.repo_context(repo.get("owner", ""), repo.get("name", ""))
    tech_line = _tech_line(str(repo.get("language") or ""), repo.get("topics") or [], languages)
    tags = _tags_for(
        repo.get("language"),
        repo.get("description"),
        " ".join(repo.get("topics") or []),
        readme[:4000],
    )
    bullets = bullets_from_readme(readme)
    db.update_github_repo_context(
        conn,
        int(repo["repo_id"]),
        readme=readme,
        languages=languages,
        tech_line=tech_line,
        tags=tags,
        suggested_bullets=bullets,
    )
    refreshed = dict(repo)
    refreshed.update(
        {
            "languages": languages,
            "readme": readme,
            "tech_line": tech_line,
            "tags": tags,
            "suggested_bullets": bullets,
        }
    )
    return refreshed
