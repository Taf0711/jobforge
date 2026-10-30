"""Agent orchestration, provider setup, candidate context, and tools."""

from .context import build_candidate_context
from .providers import (
    PROVIDERS,
    ProviderAuthError,
    ProviderClient,
    ProviderCredentialStore,
    ProviderError,
    ProviderModelError,
    ProviderRateLimitError,
    get_provider,
    infer_provider,
    normalize_provider_id,
    persist_llm_config,
    provider_status,
    resolve_provider,
)

def __getattr__(name: str):
    """Load the runtime lazily so tailoring/provider imports stay acyclic."""
    if name in {"AgentError", "AgentResult", "AgentRuntime"}:
        from . import runtime

        return getattr(runtime, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "PROVIDERS",
    "ProviderAuthError",
    "ProviderClient",
    "ProviderCredentialStore",
    "ProviderError",
    "ProviderModelError",
    "ProviderRateLimitError",
    "build_candidate_context",
    "get_provider",
    "infer_provider",
    "normalize_provider_id",
    "persist_llm_config",
    "provider_status",
    "resolve_provider",
]
