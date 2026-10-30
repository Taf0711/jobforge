"""Authorization for the read-only external workbench interface."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from .. import db
from ..config import Config
from ..models import Preferences, Profile
from .contracts import ToolError


@dataclass
class QueryContext:
    config: Config
    conn: sqlite3.Connection
    instance: dict[str, Any]
    connection: dict[str, Any]
    profile: Profile
    prefs: Preferences
    request_id: str = ""

    @property
    def scopes(self) -> set[str]:
        return {str(scope) for scope in (self.connection.get("scopes") or [])}

    @property
    def profile_id(self) -> int:
        return int(self.connection["profile_id"])

    @property
    def connection_id(self) -> str:
        return str(self.connection["id"])

    @property
    def instance_id(self) -> str:
        return str(self.instance["id"])


def load_query_context(
    config: Config,
    conn: sqlite3.Connection,
    *,
    connection_id: str,
    instance_id: str | None = None,
) -> QueryContext:
    """Resolve an enabled connection and its explicitly bound profile.

    This function never reads or changes the active profile. Connection IDs are
    selectors, not bearer credentials; the local OS account remains the trust
    boundary, as documented in docs/31.
    """
    if not config.features.agent_integrations:
        raise ToolError(
            "CONNECTION_DISABLED",
            "The local agent integration feature is disabled.",
        )
    try:
        connection = db.get_agent_connection(conn, connection_id)
    except sqlite3.OperationalError as exc:
        raise ToolError(
            "MIGRATION_REQUIRED",
            "JobForge schema is not initialized for agent connections; run human setup.",
        ) from exc
    if connection is None:
        raise ToolError("NOT_FOUND", "Connection not found.")
    if instance_id and str(connection["instance_id"]) != instance_id:
        raise ToolError("NOT_FOUND", "Connection not found for this instance.")
    if not connection.get("enabled"):
        raise ToolError("CONNECTION_DISABLED", "This connection has been disabled.")
    instance = db.get_agent_instance(conn, str(connection["instance_id"]))
    if instance is None:
        raise ToolError("NOT_FOUND", "Connection instance not found.")
    profile = db.get_profile(conn, int(connection["profile_id"]))
    if profile is None:
        raise ToolError("NOT_FOUND", "The bound profile no longer exists.")
    prefs = db.get_preferences(conn, int(connection["profile_id"]))
    return QueryContext(
        config=config,
        conn=conn,
        instance=instance,
        connection=connection,
        profile=profile,
        prefs=prefs,
    )


def require_scope(context: QueryContext, scope: str | None) -> None:
    if scope and scope not in context.scopes:
        raise ToolError(
            "SCOPE_DENIED",
            f"This connection does not grant {scope}.",
            details={"scope": scope},
        )
