"""Generic profile runner: ``ruloom run --profile <id> SOURCE ...``.

The engine executes the profile manifest graph (steps, bounded loops, and
a human gate) and enforces each capability's declared permissions. Omitting
``--profile`` runs the default profile, which preserves the original
single-profile invocation unchanged.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

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
    (default off, so the default run is unchanged).
    """
    parser = argparse.ArgumentParser(prog="ruloom")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run a profile synthesis lifecycle")
    run.add_argument(
        "source", help="source data file for the selected profile"
    )
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
        raise ManifestError(
            f"profile {profile_id!r} exposes no CAPABILITIES registry"
        )
    return capabilities


def default_client(role: str, config: Mapping[str, Any]) -> StructuredModel:
    """Build the model client for one resolved role (composition root).

    Each role binding declares its adapter: ``meta_cli`` (the hosted
    ``muse exec`` adapter, the default for existing roles) or
    ``glimmer_local`` (the local OpenAI-compatible server). An unknown
    adapter name fails loudly.
    """
    adapter = config.get("adapter", "meta_cli")
    if adapter == "glimmer_local":
        from models.glimmer import DEFAULT_BASE_URL, GlimmerLocalModel

        return GlimmerLocalModel(
            model=str(config.get("model", "muse-glimmer-30B")),
            base_url=str(config.get("base_url") or DEFAULT_BASE_URL),
        )
    if adapter != "meta_cli":
        raise ValueError(
            f"unknown model adapter {adapter!r} for role {role!r} "
            "(expected 'meta_cli' or 'glimmer_local')"
        )
    from models.meta_cli import MuseCLIModel

    return MuseCLIModel(
        model=str(config.get("model", "muse-spark-1.3")),
        effort=str(config.get("reasoning_effort", "max")),
        contributor=True,
    )


def main(
    argv: Sequence[str] | None = None,
    model: StructuredModel | None = None,
) -> int:
    """Entry point for the ``ruloom`` console script (test-injectable)."""
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.command == "run":
        if isinstance(args.max_attempts, bool) or args.max_attempts < 1:
            print(
                "error: --max-attempts must be a positive integer, "
                f"got {args.max_attempts!r}",
                file=sys.stderr,
            )
            return 1
        try:
            profile_id = resolve_profile(args.profile)
            manifest = load_manifest(
                profiles_package.manifest_path(profile_id)
            )
            registry = load_registry(profile_id)
        except (ValueError, ManifestError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        def factory(role: str, config: Mapping[str, Any]) -> Any:
            if model is not None:
                return model
            return default_client(role, config)

        code, _outcome = execute(
            manifest,
            registry,
            source=Path(args.source),
            max_attempts=args.max_attempts,
            model_factory=factory,
            out=Path(args.out),
            images_dir=Path(args.images) if args.images is not None else None,
            local_critic=args.local_critic,
        )
        return code
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
