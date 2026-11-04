"""Explicit model-family selection; transport and model are separate choices."""

from __future__ import annotations

AGY_FAMILY_DEFAULTS = {
    "claude": "claude-opus-5-5",
    "gemini": "gemini-4-argon",
    "gpt": "gpt-oss-120b",
}


def model_family(adapter: str, model: str) -> str:
    if adapter in {"meta_cli", "glimmer_local"}:
        return "meta"
    if adapter == "azure_foundry":
        if model in {"muse-spark-1.3", "muse-glimmer-30b"}:
            return "meta"
        raise ValueError(
            f"unknown azure_foundry model {model!r} (expected 'muse-spark-1.3' or 'muse-glimmer-30b')"
        )
    if adapter == "agy_cli":
        for family in AGY_FAMILY_DEFAULTS:
            if model.startswith(family + "-"):
                return family
    raise ValueError(f"cannot identify family for {adapter!r} model {model!r}")


def family_default(adapter: str, family: str, effort: str) -> str:
    """Defaults are convenience aliases, not proof of account availability."""
    if adapter != "agy_cli" or family not in AGY_FAMILY_DEFAULTS:
        raise ValueError(f"family {family!r} has no default for adapter {adapter!r}")
    if effort not in {"low", "medium", "high"}:
        raise ValueError("AGY family defaults support low, medium or high effort")
    if family == "gpt" and effort != "medium":
        raise ValueError("the configured AGY GPT default supports medium effort only")
    return f"{AGY_FAMILY_DEFAULTS[family]}-{effort}"


def validate_selection(adapter: str, model: str, family: str, effort: str) -> None:
    observed = model_family(adapter, model)
    if family != observed:
        raise ValueError(f"model {model!r} belongs to {observed!r}, not {family!r}")
    if adapter == "azure_foundry":
        if effort == "max":
            raise ValueError(
                "Azure Foundry rejects reasoning_effort 'max'; "
                "supported values: minimal, low, medium, high, xhigh"
            )
        if effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError(f"unsupported azure_foundry effort {effort!r}")
        if model not in {"muse-spark-1.3", "muse-glimmer-30b"}:
            raise ValueError(
                f"unsupported azure_foundry model {model!r} (expected 'muse-spark-1.3' or 'muse-glimmer-30b')"
            )
    if adapter == "agy_cli":
        if effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise ValueError(f"unsupported AGY effort {effort!r}")
        suffix = model.rsplit("-", 1)[-1]
        if suffix in {"low", "medium", "high", "xhigh", "max"} and suffix != effort:
            raise ValueError(f"AGY alias effort {suffix!r} conflicts with {effort!r}")
