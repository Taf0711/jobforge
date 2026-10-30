"""Install JobForge into local agent hosts."""

from .core import (
    DEFAULT_SCOPES,
    HOST_ORDER,
    InstallerError,
    apply_plan,
    build_plan,
    detect_hosts,
    doctor,
    host_specs,
)

__all__ = [
    "DEFAULT_SCOPES",
    "HOST_ORDER",
    "InstallerError",
    "apply_plan",
    "build_plan",
    "detect_hosts",
    "doctor",
    "host_specs",
]
