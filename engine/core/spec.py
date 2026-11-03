"""Versioned GenerationSpec: what one run may produce, decided up front.

A GenerationSpec is a small JSON record naming the spec id and version,
the profile, the source pin, the requested outputs, the plan strategy,
the row/attempt limits, and the output-terms intent. The engine
normalises it before any capability runs and refuses unsupported
requests loudly -- before any model call and before any generation --
leaving a durable terminal run record. The normalised spec and its
digest ride the run record, the run log, and every attempt receipt, so
the evidence always says which spec the run executed under.

Shape (all keys optional on input; absent keys take documented defaults)::

    {
        "spec_version": 1,
        "spec_id": "demo-default",
        "profile": "demo",
        "source": {"path": "<run source>", "sha256": null},
        "outputs": ["scalar"],
        "strategy": "fitted_markov_joint",
        "limits": {"max_rows": 200000, "max_attempts": 3},
        "rights": {"output_terms_intent": null},
    }

``source.sha256`` is an optional expected pin: when present, intake
verifies the measured source bytes against it before anything else.
``outputs`` names the modalities the run wants; each must be declared
by the profile manifest. ``strategy`` must be one of the manifest's
declared strategies (or null when the manifest declares none).
``limits.max_rows`` may only tighten the manifest bound, never widen
it; ``limits.max_attempts`` must equal the caller-supplied bound the
loop executes under. ``rights.output_terms_intent`` is recorded
verbatim as intent only; it never clears output by itself.

Optional ``target_rows`` pins the exact plan row count within the row limit;
``purpose`` records a user request as intent, never a verified quality claim.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..artifacts.hashing import sha256_canonical
from .manifest import GENERATION_OUTPUTS

#: The spec version this engine implements.
SPEC_VERSION = 1

#: Spec versions the engine accepts. Anything else is refused before
#: any work starts, so a future spec can never silently run here.
SUPPORTED_SPEC_VERSIONS = (SPEC_VERSION,)

#: Top-level keys a spec may carry. Anything else is refused: the
#: engine never guesses what an unknown field means.
SPEC_KEYS = (
    "spec_version",
    "spec_id",
    "profile",
    "source",
    "outputs",
    "strategy",
    "limits",
    "rights",
    "target_rows",
    "purpose",
)


class SpecError(Exception):
    """A GenerationSpec is missing, unreadable, or unsupported."""


def _is_int(value: Any) -> bool:
    """True for a genuine integer (bools are not integers here)."""
    return isinstance(value, int) and not isinstance(value, bool)


def default_spec(
    manifest: Any,
    *,
    source: Path | str,
    max_attempts: int,
    images_dir: Path | None = None,
) -> dict[str, Any]:
    """The spec the current CLI flags imply (no spec file given).

    Scalar output is always requested; the render branch is requested
    exactly when the caller supplies an image directory (a profile
    without that output then refuses loudly instead of silently
    dropping the request). The strategy defaults to the manifest's
    declared default, and the row bound defaults to the manifest's
    own maximum (null when the manifest declares none).
    """
    generation = getattr(manifest, "generation", None)
    if not isinstance(generation, Mapping):
        generation = {}
    strategies = generation.get("strategies", [])
    if not isinstance(strategies, list):
        strategies = []
    default_strategy = generation.get(
        "default_strategy", strategies[0] if strategies else None
    )
    manifest_max: Any = None
    limits = getattr(manifest, "limits", None)
    if isinstance(limits, Mapping):
        manifest_max = limits.get("max_rows")
    if not _is_int(manifest_max) or manifest_max < 1:
        manifest_max = None
    return {
        "spec_version": SPEC_VERSION,
        "spec_id": f"{manifest.id}-default",
        "profile": manifest.id,
        "source": {"path": str(source), "sha256": None},
        "outputs": (["scalar", "imaging"] if images_dir is not None else ["scalar"]),
        "strategy": default_strategy,
        "limits": {"max_rows": manifest_max, "max_attempts": max_attempts},
        "rights": {"output_terms_intent": None},
    }


def load_spec_file(path: str | Path) -> dict[str, Any]:
    """Read a spec overlay from ``path`` (must decode to an object)."""
    spec_path = Path(path)
    try:
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SpecError(f"spec file not found: {spec_path}") from exc
    except json.JSONDecodeError as exc:
        raise SpecError(f"spec file {spec_path} is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise SpecError(f"cannot read spec file {spec_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SpecError(
            f"spec file {spec_path} must decode to an object, "
            f"got {type(payload).__name__}: refusing (no run)"
        )
    return payload


def _merged(base: dict[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Overlay a spec file onto the flag-implied defaults.

    Top-level keys replace; the ``source``, ``limits``, and ``rights``
    mappings merge key-wise so a file can pin one field without
    restating the rest.
    """
    merged = {key: value for key, value in base.items()}
    for key, value in overlay.items():
        if (
            key in ("source", "limits", "rights")
            and isinstance(value, Mapping)
            and isinstance(merged.get(key), Mapping)
        ):
            nested = dict(merged[key])
            nested.update(dict(value))
            merged[key] = nested
        else:
            merged[key] = value
    return merged


def _refuse(what: str) -> SpecError:
    """Build the refusal error for an unsupported spec."""
    return SpecError(f"{what}: refusing (no run)")


def normalize_spec(
    overlay: Mapping[str, Any] | None,
    manifest: Any,
    *,
    source: Path | str,
    max_attempts: int,
    images_dir: Path | None = None,
) -> tuple[dict[str, Any], str]:
    """Merge ``overlay`` onto the flag-implied defaults and validate.

    Returns ``(spec, digest)``: the normalised spec plus its canonical
    SHA-256, which the engine records as the run's spec evidence.
    Every unsupported request -- unknown profile, unknown output
    modality, unknown strategy, unsupported spec version, rows over
    the declared limit, an output the profile never declared, a source
    pin pointing elsewhere, or an attempt bound that contradicts the
    caller's -- raises :class:`SpecError` before any model call and
    before any generation.
    """
    if overlay is None:
        merged = default_spec(
            manifest,
            source=source,
            max_attempts=max_attempts,
            images_dir=images_dir,
        )
    else:
        if not isinstance(overlay, Mapping):
            raise _refuse(f"spec must be a mapping, got {type(overlay).__name__}")
        unknown = [key for key in overlay if key not in SPEC_KEYS]
        if unknown:
            raise _refuse(f"spec carries unknown keys {sorted(unknown)}")
        merged = _merged(
            default_spec(
                manifest,
                source=source,
                max_attempts=max_attempts,
                images_dir=images_dir,
            ),
            overlay,
        )

    version = merged.get("spec_version", SPEC_VERSION)
    if not _is_int(version) or version not in SUPPORTED_SPEC_VERSIONS:
        raise _refuse(
            f"unsupported spec version {version!r} "
            f"(engine supports {list(SUPPORTED_SPEC_VERSIONS)})"
        )
    merged["spec_version"] = version

    spec_id = merged.get("spec_id", f"{manifest.id}-default")
    if not isinstance(spec_id, str) or not spec_id:
        raise _refuse("spec 'spec_id' must be a non-empty string")
    merged["spec_id"] = spec_id

    profile = merged.get("profile", manifest.id)
    if not isinstance(profile, str) or not profile:
        raise _refuse("spec 'profile' must be a non-empty string")
    if profile != manifest.id:
        raise _refuse(
            f"spec profile {profile!r} does not match run profile {manifest.id!r}"
        )
    merged["profile"] = profile

    generation = getattr(manifest, "generation", None)
    if not isinstance(generation, Mapping):
        generation = {}
    declared_strategies = generation.get("strategies", [])
    if not isinstance(declared_strategies, list):
        declared_strategies = []
    declared_outputs = generation.get("outputs", ["scalar"])
    if not isinstance(declared_outputs, list) or not declared_outputs:
        declared_outputs = ["scalar"]

    outputs = merged.get("outputs", ["scalar"])
    if (
        not isinstance(outputs, list)
        or not outputs
        or any(not isinstance(name, str) for name in outputs)
    ):
        raise _refuse("spec 'outputs' must be a non-empty list of strings")
    for name in outputs:
        if name not in GENERATION_OUTPUTS:
            raise _refuse(
                f"unknown output modality {name!r} (known: {list(GENERATION_OUTPUTS)})"
            )
        if name not in declared_outputs:
            raise _refuse(
                f"output {name!r} requested but profile "
                f"{manifest.id!r} declares {list(declared_outputs)}"
            )
    merged["outputs"] = list(outputs)

    strategy = merged.get("strategy")
    if declared_strategies:
        if strategy not in declared_strategies:
            raise _refuse(
                f"unknown strategy {strategy!r} "
                f"(profile {manifest.id!r} declares "
                f"{list(declared_strategies)})"
            )
    elif strategy is not None:
        raise _refuse(
            f"unknown strategy {strategy!r} "
            f"(profile {manifest.id!r} declares no named strategies)"
        )
    merged["strategy"] = strategy

    manifest_max: Any = None
    limits = getattr(manifest, "limits", None)
    if isinstance(limits, Mapping):
        manifest_max = limits.get("max_rows")
    if not _is_int(manifest_max) or manifest_max < 1:
        manifest_max = None
    raw_limits = merged.get("limits", {})
    if raw_limits is None:
        raw_limits = {}
    if not isinstance(raw_limits, Mapping):
        raise _refuse("spec 'limits' must be an object")
    spec_max = raw_limits.get("max_rows", manifest_max)
    if spec_max is not None:
        if not _is_int(spec_max) or spec_max < 1:
            raise _refuse("spec 'limits.max_rows' must be a positive integer")
        if manifest_max is not None and spec_max > manifest_max:
            raise _refuse(
                f"spec rows {spec_max} exceed manifest limit max_rows {manifest_max}"
            )
    spec_attempts = raw_limits.get("max_attempts", max_attempts)
    if not _is_int(spec_attempts) or spec_attempts < 1:
        raise _refuse("spec 'limits.max_attempts' must be a positive integer")
    if spec_attempts != max_attempts:
        raise _refuse(
            f"spec max_attempts {spec_attempts} contradicts the caller "
            f"bound {max_attempts}"
        )
    merged["limits"] = {"max_rows": spec_max, "max_attempts": spec_attempts}

    if "target_rows" in merged:
        target = merged["target_rows"]
        if (
            not _is_int(target)
            or target < 1
            or (spec_max is not None and target > spec_max)
        ):
            raise _refuse(
                "spec 'target_rows' must be a positive integer within max_rows"
            )
    if "purpose" in merged:
        purpose = merged["purpose"]
        if not isinstance(purpose, str) or not purpose.strip() or len(purpose) > 4000:
            raise _refuse("spec 'purpose' must contain 1 to 4000 characters")

    raw_rights = merged.get("rights", {})
    if raw_rights is None:
        raw_rights = {}
    if not isinstance(raw_rights, Mapping):
        raise _refuse("spec 'rights' must be an object")
    intent = raw_rights.get("output_terms_intent")
    if intent is not None and (not isinstance(intent, str) or not intent.strip()):
        raise _refuse(
            "spec 'rights.output_terms_intent' must be a non-empty string when present"
        )
    merged["rights"] = {"output_terms_intent": intent}

    raw_source = merged.get("source", {})
    if raw_source is None:
        raw_source = {}
    if not isinstance(raw_source, Mapping):
        raise _refuse("spec 'source' must be an object")
    pinned_path = raw_source.get("path", str(source))
    if not isinstance(pinned_path, str) or not pinned_path:
        raise _refuse("spec 'source.path' must be a non-empty string")
    if pinned_path != str(source):
        raise _refuse(
            f"spec source path {pinned_path!r} does not match run "
            f"source {str(source)!r}"
        )
    pin = raw_source.get("sha256")
    if pin is not None and (not isinstance(pin, str) or not pin):
        raise _refuse("spec 'source.sha256' must be a non-empty string")
    merged["source"] = {"path": str(source), "sha256": pin}

    return merged, sha256_canonical(merged)
