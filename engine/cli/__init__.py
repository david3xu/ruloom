"""Generic profile runner: ``ruloom run --profile <id> SOURCE ...``.

The engine executes the profile manifest graph (steps, bounded loops, and
a human gate) and enforces each capability's declared permissions. Omitting
``--profile`` runs the default profile, which preserves the original
single-profile invocation unchanged. Every run executes under a
versioned GenerationSpec: the CLI flags imply a default spec, and
``--spec PATH`` overlays a spec file on those defaults. Unsupported
requests are refused before any model call with a durable terminal
record under ``--out``.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import profiles as profiles_package
from models.base import StructuredModel

from ..core.manifest import ManifestError, load_manifest
from ..core.runner import execute


def build_parser() -> argparse.ArgumentParser:
    """CLI parser: ``ruloom run [--profile ID] SOURCE --max-attempts N``.

    ``--images DIR`` is optional: a directory of companion image files
    for profiles declaring image capabilities. Without it the run is
    unchanged (no image step does any work). ``--local-critic`` is
    likewise optional: it enables the advisory local text-critique step
    (default off, so the default run is unchanged). ``--spec PATH``
    overlays a GenerationSpec JSON file on the flag-implied defaults;
    without it the flags alone define the spec, so existing invocations
    behave exactly as before. ``--plan PATH`` replays a saved plan JSON
    file (a ``plan.json`` from an earlier run) instead of requesting a
    new plan: the saved seed and row count are validated against the run
    spec and reused, with no planner call.
    """
    parser = argparse.ArgumentParser(prog="ruloom")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a profile synthesis lifecycle")
    run.add_argument("source", help="source data file for the selected profile")
    run.add_argument(
        "--profile",
        default=None,
        help="profile id (default: the compatibility profile)",
    )
    run.add_argument(
        "--max-attempts",
        required=True,
        type=int,
        help="bound on generation attempts (required, no default)",
    )
    run.add_argument(
        "--out",
        default=".",
        help="base output directory (artifact goes to <out>/runs/<sha>/)",
    )
    run.add_argument(
        "--images",
        default=None,
        help="optional directory of companion image files",
    )
    run.add_argument(
        "--local-critic",
        action="store_true",
        default=False,
        help="enable the optional advisory local text-critique step",
    )
    run.add_argument(
        "--spec",
        default=None,
        help="path to a GenerationSpec JSON file overriding defaults",
    )
    run.add_argument(
        "--plan",
        default=None,
        help="path to a saved plan JSON file to replay (no planner call)",
    )
    run.add_argument(
        "--role-model",
        action="append",
        default=[],
        metavar="ROLE=MODEL",
        help="override a role's model id (repeatable; beats env/profile)",
    )
    run.add_argument(
        "--role-effort",
        action="append",
        default=[],
        metavar="ROLE=EFFORT",
        help="override a role's reasoning effort (repeatable)",
    )
    run.add_argument(
        "--role-base-url",
        action="append",
        default=[],
        metavar="ROLE=URL",
        help="override a local role's base URL (repeatable)",
    )
    run.add_argument(
        "--role-adapter",
        action="append",
        default=[],
        metavar="ROLE=ADAPTER",
        help="explicit adapter: meta_cli, agy_cli, glimmer_local or azure_foundry (repeatable)",
    )
    run.add_argument(
        "--role-family",
        action="append",
        default=[],
        metavar="ROLE=FAMILY",
        help="explicit family: claude, gemini or gpt via agy_cli; meta via Muse/local/azure",
    )
    run.add_argument(
        "--azure-subscription-id",
        "--subscription",
        default=None,
        metavar="UUID",
        help="explicit Azure subscription ID (canonical UUID)",
    )
    run.add_argument(
        "--model-policy",
        choices=("hosted", "offline", "local-only", "azure-only"),
        default="hosted",
        help="model-call policy, including repair and critique",
    )
    prepare = sub.add_parser(
        "prepare", help="draft supported settings for human review"
    )
    prepare.add_argument(
        "source", help="source file, hashed locally but never sent to model"
    )
    prepare.add_argument("--profile", default=None)
    prepare.add_argument(
        "--request", required=True, help="short request; exclude private rows"
    )
    prepare.add_argument("--max-attempts", required=True, type=int)
    prepare.add_argument(
        "--out", required=True, help="new directory for draft and receipt"
    )
    prepare.add_argument(
        "--model-policy",
        choices=("hosted", "local-only", "azure-only"),
        default="local-only",
    )
    prepare.add_argument("--role-model", action="append", default=[])
    prepare.add_argument("--role-effort", action="append", default=[])
    prepare.add_argument("--role-base-url", action="append", default=[])
    prepare.add_argument("--role-adapter", action="append", default=[])
    prepare.add_argument("--role-family", action="append", default=[])
    prepare.add_argument(
        "--azure-subscription-id",
        "--subscription",
        default=None,
        metavar="UUID",
        help="explicit Azure subscription ID (canonical UUID)",
    )
    return parser


def resolve_profile(profile_id: str | None) -> str:
    """Return the requested profile id or the compatibility default."""
    if profile_id is not None:
        return profile_id
    return profiles_package.DEFAULT_PROFILE_ID


def load_registry(profile_id: str) -> Mapping[str, Any]:
    """Import the capability implementations for ``profile_id``."""
    profiles_package.profile_dir(profile_id)
    module = importlib.import_module(f"profiles.{profile_id}.capabilities")
    capabilities = getattr(module, "CAPABILITIES", None)
    if not isinstance(capabilities, Mapping) or not capabilities:
        raise ManifestError(f"profile {profile_id!r} exposes no CAPABILITIES registry")
    return capabilities


#: Engine defaults applied when neither CLI, environment, nor manifest
#: supplies a value for a role field.
DEFAULT_MODEL_BY_ADAPTER = {
    "meta_cli": "muse-spark-1.3",
    "glimmer_local": "muse-glimmer-30B",
    "agy_cli": "claude-opus-5-5-high",
    "azure_foundry": "muse-spark-1.3",
}
DEFAULT_EFFORT = "max"

#: Override precedence for role fields (highest first): CLI flags, then
#: ``RULOOM_ROLE_<ROLE>_<FIELD>`` environment variables, then the manifest
#: binding, then the adapter default. The composition root records which
#: level supplied every effective value in the run log.
SOURCE_CLI = "cli"
SOURCE_ENV = "env"
SOURCE_PROFILE = "profile"
SOURCE_DEFAULT = "default"

#: Environment variable suffixes per role field (role name uppercased).
ENV_SUFFIXES = {
    "adapter": "ADAPTER",
    "model": "MODEL",
    "reasoning_effort": "EFFORT",
    "base_url": "BASE_URL",
}


def parse_role_assignment(spec: str, flag: str) -> tuple[str, str]:
    """Split one ``ROLE=VALUE`` CLI override; a missing ``=`` fails loudly."""
    if "=" not in spec:
        raise ValueError(f"{flag} expects ROLE=VALUE, got {spec!r}")
    role, _, value = spec.partition("=")
    role = role.strip().lower()
    value = value.strip()
    if not role or not value:
        raise ValueError(f"{flag} expects ROLE=VALUE, got {spec!r}")
    return role, value


def resolve_role_bindings(
    manifest: Any,
    *,
    cli_models: Sequence[str] | None = None,
    cli_efforts: Sequence[str] | None = None,
    cli_base_urls: Sequence[str] | None = None,
    cli_adapters: Sequence[str] | None = None,
    cli_families: Sequence[str] | None = None,
    cli_subscription_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    """Apply CLI > env > profile > default precedence to every role.

    Returns ``(resolved, sources)``: the effective binding per role and,
    per role, the source level (``cli``/``env``/``profile``/``default``)
    that supplied each field. Unknown roles in CLI overrides fail loudly;
    so does a resolved open-weight requirement on the hosted adapter.
    """
    import os as _os

    environment: Mapping[str, str] = _os.environ if env is None else env
    cli_model_map: dict[str, str] = {}
    for spec in cli_models or []:
        role, value = parse_role_assignment(spec, "--role-model")
        cli_model_map[role] = value
    cli_effort_map: dict[str, str] = {}
    for spec in cli_efforts or []:
        role, value = parse_role_assignment(spec, "--role-effort")
        cli_effort_map[role] = value
    cli_base_url_map: dict[str, str] = {}
    for spec in cli_base_urls or []:
        role, value = parse_role_assignment(spec, "--role-base-url")
        cli_base_url_map[role] = value
    cli_adapter_map = dict(
        parse_role_assignment(value, "--role-adapter") for value in cli_adapters or []
    )

    cli_family_map = dict(
        parse_role_assignment(value, "--role-family") for value in cli_families or []
    )
    declared = manifest.models.get("roles", {})
    if not isinstance(declared, dict):
        declared = {}
    for role in (
        list(cli_model_map)
        + list(cli_effort_map)
        + list(cli_base_url_map)
        + list(cli_adapter_map)
        + list(cli_family_map)
    ):
        if role not in declared:
            raise ManifestError(
                f"role override for unknown role {role!r}: the manifest "
                "binds " + ", ".join(sorted(declared))
            )
    resolved: dict[str, dict[str, Any]] = {}
    sources: dict[str, dict[str, str]] = {}
    for role, binding in declared.items():
        base = dict(binding) if isinstance(binding, Mapping) else {}
        adapter = base.get("adapter", "meta_cli")
        role_sources: dict[str, str] = {}
        original_adapter = adapter
        adapter_env = f"RULOOM_ROLE_{role.upper()}_ADAPTER"
        if role in cli_adapter_map:
            adapter = cli_adapter_map[role]
            role_sources["adapter"] = SOURCE_CLI
        elif adapter_env in environment:
            adapter = environment[adapter_env]
            role_sources["adapter"] = SOURCE_ENV
        else:
            role_sources["adapter"] = SOURCE_PROFILE
        if adapter not in DEFAULT_MODEL_BY_ADAPTER:
            raise ManifestError(f"unknown model adapter {adapter!r} for role {role!r}")
        base["adapter"] = adapter
        if adapter != original_adapter:
            base["provider"] = {
                "glimmer_local": "local",
                "meta_cli": "meta-api",
                "agy_cli": "agy",
                "azure_foundry": "azure",
            }[adapter]

        if role in cli_model_map:
            base["model"] = cli_model_map[role]
            role_sources["model"] = SOURCE_CLI
        elif f"RULOOM_ROLE_{role.upper()}_MODEL" in environment:
            base["model"] = environment[f"RULOOM_ROLE_{role.upper()}_MODEL"]
            role_sources["model"] = SOURCE_ENV
        elif (
            adapter == original_adapter
            and isinstance(binding, Mapping)
            and binding.get("model")
        ):
            role_sources["model"] = SOURCE_PROFILE
        else:
            base["model"] = DEFAULT_MODEL_BY_ADAPTER.get(adapter, "muse-spark-1.3")
            role_sources["model"] = SOURCE_DEFAULT

        if role in cli_effort_map:
            base["reasoning_effort"] = cli_effort_map[role]
            role_sources["reasoning_effort"] = SOURCE_CLI
        elif f"RULOOM_ROLE_{role.upper()}_EFFORT" in environment:
            base["reasoning_effort"] = environment[f"RULOOM_ROLE_{role.upper()}_EFFORT"]
            role_sources["reasoning_effort"] = SOURCE_ENV
        elif (
            adapter == original_adapter
            and isinstance(binding, Mapping)
            and binding.get("reasoning_effort")
        ):
            role_sources["reasoning_effort"] = SOURCE_PROFILE
        else:
            base["reasoning_effort"] = (
                "xhigh"
                if adapter == "azure_foundry"
                else ("high" if adapter == "agy_cli" else DEFAULT_EFFORT)
            )
            role_sources["reasoning_effort"] = SOURCE_DEFAULT

        if adapter in {"glimmer_local", "azure_foundry"}:
            if role in cli_base_url_map:
                base["base_url"] = cli_base_url_map[role]
                role_sources["base_url"] = SOURCE_CLI
            elif f"RULOOM_ROLE_{role.upper()}_BASE_URL" in environment:
                base["base_url"] = environment[f"RULOOM_ROLE_{role.upper()}_BASE_URL"]
                role_sources["base_url"] = SOURCE_ENV
            elif isinstance(binding, Mapping) and binding.get("base_url"):
                role_sources["base_url"] = SOURCE_PROFILE
            elif adapter == "glimmer_local":
                from models.glimmer import DEFAULT_BASE_URL

                base["base_url"] = DEFAULT_BASE_URL
                role_sources["base_url"] = SOURCE_DEFAULT
            else:
                raise ManifestError(
                    f"role {role!r} bound to adapter 'azure_foundry' requires explicit base_url"
                )
            if adapter == "azure_foundry":
                from models.azure_foundry import validate_azure_base_url

                try:
                    base["base_url"] = validate_azure_base_url(base["base_url"])
                except ValueError as exc:
                    raise ManifestError(str(exc)) from exc

        if adapter == "azure_foundry":
            sub_env = "RULOOM_AZURE_SUBSCRIPTION_ID"
            role_sub_env = f"RULOOM_ROLE_{role.upper()}_SUBSCRIPTION_ID"
            if cli_subscription_id:
                base["subscription_id"] = cli_subscription_id
                role_sources["subscription_id"] = SOURCE_CLI
            elif role_sub_env in environment:
                base["subscription_id"] = environment[role_sub_env]
                role_sources["subscription_id"] = SOURCE_ENV
            elif sub_env in environment:
                base["subscription_id"] = environment[sub_env]
                role_sources["subscription_id"] = SOURCE_ENV
            elif isinstance(binding, Mapping) and binding.get("subscription_id"):
                role_sources["subscription_id"] = SOURCE_PROFILE
            else:
                raise ManifestError(
                    f"role {role!r} bound to adapter 'azure_foundry' requires explicit subscription_id"
                )
            from models.azure_foundry import validate_subscription_id

            try:
                base["subscription_id"] = validate_subscription_id(
                    base["subscription_id"]
                )
            except ValueError as exc:
                raise ManifestError(str(exc)) from exc

        family_env = f"RULOOM_ROLE_{role.upper()}_FAMILY"
        family = cli_family_map.get(role, environment.get(family_env))
        if family is None and adapter == original_adapter:
            family = base.get("family")
        if family is not None or adapter in {"agy_cli", "azure_foundry"}:
            from models.selection import (
                family_default,
                model_family,
                validate_selection,
            )

            if (
                family is not None
                and role not in cli_model_map
                and f"RULOOM_ROLE_{role.upper()}_MODEL" not in environment
            ):
                if adapter == "agy_cli" and (
                    adapter != original_adapter or not binding.get("model")
                ):
                    base["model"] = family_default(
                        adapter, family, base["reasoning_effort"]
                    )
            base["family"] = family or model_family(adapter, base["model"])
            role_sources["family"] = (
                SOURCE_CLI
                if role in cli_family_map
                else SOURCE_ENV
                if family_env in environment
                else SOURCE_PROFILE
                if family
                else "inferred"
            )
            validate_selection(
                adapter, base["model"], base["family"], base["reasoning_effort"]
            )

        requires = base.get("requires", {})
        if (
            isinstance(requires, Mapping)
            and requires.get("open_weight") is True
            and base.get("adapter", "meta_cli") != "glimmer_local"
        ):
            raise ManifestError(
                f"role {role!r} requires open_weight but resolves to the "
                "hosted adapter: refusing (no run)"
            )
        resolved[role] = base
        sources[role] = role_sources
    return resolved, sources


def default_client(role: str, config: Mapping[str, Any]) -> StructuredModel:
    """Build the model client for one resolved role (composition root).

    Each role binding declares its adapter: ``meta_cli`` (the hosted
    ``muse exec`` adapter, the default for existing roles) or
    ``glimmer_local`` (the local OpenAI-compatible server), or ``agy_cli``
    (AGY with an explicit model family). An unknown
    adapter name fails loudly, as does an open-weight requirement bound
    to the hosted adapter (fail-closed: the refusal happens here, before
    any model call, even for programmatically built configs).
    """
    adapter = config.get("adapter", "meta_cli")
    if adapter == "agy_cli" or config.get("family") is not None:
        from models.selection import model_family, validate_selection

        selected_model = str(
            config.get("model", DEFAULT_MODEL_BY_ADAPTER.get(adapter, ""))
        )
        selected_effort = str(
            config.get(
                "reasoning_effort", "high" if adapter == "agy_cli" else DEFAULT_EFFORT
            )
        )
        selected_family = str(
            config.get("family") or model_family(adapter, selected_model)
        )
        validate_selection(adapter, selected_model, selected_family, selected_effort)
    requires = config.get("requires", {})
    if (
        isinstance(requires, Mapping)
        and requires.get("open_weight") is True
        and adapter != "glimmer_local"
    ):
        raise ValueError(
            f"role {role!r} requires open_weight but is bound to hosted "
            f"adapter {adapter!r}: refusing (no run)"
        )
    if adapter == "glimmer_local":
        from models.glimmer import DEFAULT_BASE_URL, GlimmerLocalModel

        return GlimmerLocalModel(
            model=str(config.get("model", "muse-glimmer-30B")),
            base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
            local_only=config.get("local_only") is True,
        )
    if adapter == "agy_cli":
        from models.agy_cli import AgyCLIModel

        return AgyCLIModel(
            model=str(config.get("model", "claude-opus-5-5-high")),
            effort=str(config.get("reasoning_effort", "high")),
        )
    if adapter == "azure_foundry":
        from models.azure_foundry import AzureFoundryModel

        base_url = config.get("base_url")
        if not base_url:
            raise ValueError(
                f"role {role!r} bound to adapter 'azure_foundry' requires explicit base_url"
            )
        subscription_id = config.get("subscription_id")
        if not subscription_id:
            raise ValueError(
                f"role {role!r} bound to adapter 'azure_foundry' requires explicit subscription_id"
            )
        return AzureFoundryModel(
            base_url=str(base_url),
            subscription_id=str(subscription_id),
            model=str(config.get("model", "muse-spark-1.3")),
            effort=str(config.get("reasoning_effort", "xhigh")),
        )
    if adapter != "meta_cli":
        raise ValueError(
            f"unknown model adapter {adapter!r} for role {role!r} "
            "(expected 'meta_cli', 'agy_cli', 'glimmer_local' or 'azure_foundry')"
        )
    from models.meta_cli import MuseCLIModel

    return MuseCLIModel(
        model=str(config.get("model", "muse-spark-1.3")),
        effort=str(config.get("reasoning_effort", "max")),
        contributor=True,
    )


def _refusal_request(args: Any) -> dict[str, Any]:
    """Best-effort request record for a refusal that precedes any run."""
    return {
        "source": str(getattr(args, "source", "")),
        "max_attempts": getattr(args, "max_attempts", None),
        "out": str(getattr(args, "out", ".")),
        "model_policy": getattr(args, "model_policy", "hosted"),
    }


def _load_plan_file(path: str) -> dict[str, Any]:
    """Read a saved plan file (must decode to a JSON object)."""
    import json as _json

    raw_path = Path(path)
    try:
        payload = _json.loads(raw_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"plan file not found: {path}") from exc
    except _json.JSONDecodeError as exc:
        raise ValueError(f"plan file {path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read plan file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(
            f"plan file {path} must decode to an object, "
            f"got {type(payload).__name__}: refusing (no run)"
        )
    return dict(payload)


def main(
    argv: Sequence[str] | None = None,
    model: StructuredModel | None = None,
) -> int:
    """Entry point for the ``ruloom`` console script (test-injectable)."""
    from ..core.runstore import record_refusal
    from ..core.spec import SpecError, load_spec_file

    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "prepare":
        from .request import prepare_request

        try:
            profile_id = resolve_profile(args.profile)
            manifest = load_manifest(profiles_package.manifest_path(profile_id))
            resolved, _sources = resolve_role_bindings(
                manifest,
                cli_models=args.role_model,
                cli_efforts=args.role_effort,
                cli_base_urls=args.role_base_url,
                cli_adapters=args.role_adapter,
                cli_families=args.role_family,
                cli_subscription_id=getattr(args, "azure_subscription_id", None),
            )
            config = dict(resolved["planner"])
            config["local_only"] = args.model_policy == "local-only"
            return prepare_request(
                args,
                manifest,
                config,
                lambda: (
                    model if model is not None else default_client("planner", config)
                ),
            )
        except (ValueError, ManifestError, KeyError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.command == "run":
        if isinstance(args.max_attempts, bool) or args.max_attempts < 1:
            print(
                "error: --max-attempts must be a positive integer, "
                f"got {args.max_attempts!r}",
                file=sys.stderr,
            )
            return 1
        try:
            if args.model_policy == "offline" and args.plan is None:
                raise ValueError(
                    "--model-policy offline requires --plan PATH; no model calls allowed"
                )
            profile_id = resolve_profile(args.profile)
            manifest = load_manifest(profiles_package.manifest_path(profile_id))
            registry = load_registry(profile_id)
            resolved, role_sources = resolve_role_bindings(
                manifest,
                cli_models=args.role_model,
                cli_efforts=args.role_effort,
                cli_base_urls=args.role_base_url,
                cli_adapters=args.role_adapter,
                cli_families=args.role_family,
                cli_subscription_id=getattr(args, "azure_subscription_id", None),
            )
        except (ValueError, ManifestError) as exc:
            try:
                record_refusal(
                    Path(args.out),
                    profile_ref=resolve_profile(args.profile),
                    request=_refusal_request(args),
                    error=str(exc),
                )
            except OSError:
                pass
            print(f"error: {exc}", file=sys.stderr)
            return 1
        spec_overlay: Mapping[str, Any] | None = None
        if args.spec is not None:
            try:
                spec_overlay = load_spec_file(args.spec)
            except SpecError as exc:
                try:
                    record_refusal(
                        Path(args.out),
                        profile_ref=profile_id,
                        request={
                            **_refusal_request(args),
                            "spec_file": str(args.spec),
                        },
                        error=str(exc),
                    )
                except OSError:
                    pass
                print(f"error: {exc}", file=sys.stderr)
                return 1

        fixed_plan: Mapping[str, Any] | None = None
        if args.plan is not None:
            try:
                fixed_plan = _load_plan_file(args.plan)
            except ValueError as exc:
                try:
                    record_refusal(
                        Path(args.out),
                        profile_ref=profile_id,
                        request={
                            **_refusal_request(args),
                            "plan_file": str(args.plan),
                        },
                        error=str(exc),
                    )
                except OSError:
                    pass
                print(f"error: {exc}", file=sys.stderr)
                return 1

        def resolving_factory(role: str, config: Mapping[str, Any]) -> Any:
            if model is not None:
                return model
            return default_client(role, config)

        code, _outcome = execute(
            manifest,
            registry,
            source=Path(args.source),
            max_attempts=args.max_attempts,
            model_factory=resolving_factory,
            out=Path(args.out),
            images_dir=Path(args.images) if args.images is not None else None,
            local_critic=args.local_critic,
            role_bindings=resolved,
            role_binding_sources=role_sources,
            spec=spec_overlay,
            fixed_plan=fixed_plan,
            model_policy=args.model_policy,
        )
        return code
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
