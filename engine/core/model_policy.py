"""Which model calls may leave the runner under the selected policy?"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from models.local_transport import require_loopback_url

MODEL_POLICIES = ("hosted", "offline", "local-only", "azure-only")


class ModelPolicyError(ValueError):
    """A call was refused before constructing or contacting its adapter."""


def validate_policy(policy: str) -> None:
    if policy not in MODEL_POLICIES:
        raise ModelPolicyError(f"unknown model policy {policy!r}")


def enforce_model_policy(policy: str, config: Mapping[str, Any]) -> None:
    """Fail closed; local-only means a literal loopback HTTP(S) endpoint."""
    validate_policy(policy)
    if policy == "offline":
        raise ModelPolicyError("offline policy forbids every model call; no fallback")
    if policy == "hosted":
        return
    if policy == "azure-only":
        if config.get("adapter") != "azure_foundry":
            raise ModelPolicyError(
                "azure-only policy refuses any non-Azure adapter; "
                "Meta account and other transports forbidden"
            )
        return
    if config.get("adapter") != "glimmer_local":
        raise ModelPolicyError("local-only policy refuses a non-local adapter")
    try:
        require_loopback_url(str(config.get("base_url", "")))
    except ValueError as exc:
        raise ModelPolicyError(str(exc)) from exc
