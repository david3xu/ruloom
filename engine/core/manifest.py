"""Declarative profile manifests: loading and validation.

A manifest is a JSON document (standard library only) that declares what a
profile is: its source reader, its capabilities with permissions, the checks
it runs with their threshold sources, model role bindings, limits, and the
stage graph. The graph uses only three node kinds: ``step``, ``loop`` and
``gate``. A ``loop`` node is bounded by the caller-supplied ``max_attempts``
binding and declares no default of its own.

The engine never interprets domain content; it validates the shape of the
declaration and enforces it mechanically at run time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..artifacts.hashing import sha256_canonical

#: The engine API version this engine implements. A manifest declaring any
#: other version is rejected rather than run against a mismatched contract.
ENGINE_API = 1

#: The only graph node kinds the engine executes.
NODE_KINDS = ("step", "loop", "gate")

#: The only permission keys a capability may declare.
PERMISSION_KEYS = (
    "reads_raw_source",
    "calls_model",
    "advisory",
    "writes_verdict",
)

#: Binding name for the caller-supplied attempt bound. Loop nodes reference
#: it; neither the manifest nor the engine supplies a default value.
MAX_ATTEMPTS_BINDING = "max_attempts"

#: Adapters the composition root knows how to build. A role binding that
#: declares ``requires.open_weight`` must use the local adapter: an open
#: weight requirement bound to the hosted adapter is refused at load.
HOSTED_ADAPTER = "meta_cli"
LOCAL_ADAPTER = "glimmer_local"


class ManifestError(Exception):
    """A profile manifest is missing, unreadable, or invalid."""


@dataclass(frozen=True)
class Manifest:
    """A validated profile manifest plus its content digest."""

    id: str
    version: int
    engine_api: int
    source: dict[str, Any]
    capabilities: dict[str, dict[str, Any]]
    graph: list[dict[str, Any]]
    checks: list[dict[str, Any]]
    models: dict[str, Any]
    limits: dict[str, Any]
    digest: str = ""
    path: str = ""


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if not isinstance(mapping, Mapping) or key not in mapping:
        raise ManifestError(f"{where}: missing required key {key!r}")
    return mapping[key]


def _check_capability(
    cap_id: str, spec: Any, check_ids: set[str]
) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ManifestError(f"capability {cap_id!r}: must be an object")
    if not isinstance(spec.get("impl"), str) or not spec["impl"]:
        raise ManifestError(f"capability {cap_id!r}: missing 'impl' reference")
    for key in spec:
        if key in ("impl", "stage"):
            continue
        if key not in PERMISSION_KEYS:
            raise ManifestError(
                f"capability {cap_id!r}: unknown permission {key!r}"
            )
    calls_model = spec.get("calls_model")
    if calls_model is not None and (
        not isinstance(calls_model, str) or not calls_model
    ):
        raise ManifestError(
            f"capability {cap_id!r}: 'calls_model' must be a role name or null"
        )
    for flag in ("reads_raw_source", "advisory", "writes_verdict"):
        if flag in spec and not isinstance(spec[flag], bool):
            raise ManifestError(
                f"capability {cap_id!r}: {flag!r} must be true or false"
            )
    _ = check_ids
    return dict(spec)


def _check_graph(
    graph: Any, capabilities: Mapping[str, Any], manifest_id: str
) -> list[dict[str, Any]]:
    if not isinstance(graph, list) or not graph:
        raise ManifestError(f"manifest {manifest_id!r}: 'graph' must be a "
                            "non-empty list")
    nodes: list[dict[str, Any]] = []
    for index, node in enumerate(graph):
        where = f"manifest {manifest_id!r} graph node {index}"
        if not isinstance(node, dict):
            raise ManifestError(f"{where}: must be an object")
        kinds = [key for key in node if key in NODE_KINDS]
        if len(kinds) != 1:
            raise ManifestError(
                f"{where}: must declare exactly one of step/loop/gate, "
                f"got {sorted(node)}"
            )
        kind = kinds[0]
        if kind == "step":
            target = node["step"]
            if not isinstance(target, str) or target not in capabilities:
                raise ManifestError(
                    f"{where}: unknown capability {target!r}"
                )
        elif kind == "gate":
            target = node["gate"]
            if not isinstance(target, str) or not target:
                raise ManifestError(f"{where}: gate needs a non-empty id")
        else:
            body = node.get("loop")
            if (
                not isinstance(body, list)
                or not body
                or any(
                    not isinstance(item, str) or item not in capabilities
                    for item in body
                )
            ):
                raise ManifestError(
                    f"{where}: loop must list a non-empty body of "
                    "declared capabilities"
                )
            on_fail = node.get("on_fail")
            if not isinstance(on_fail, str) or on_fail not in capabilities:
                raise ManifestError(
                    f"{where}: loop needs an 'on_fail' capability"
                )
            bound = node.get("max_attempts")
            if bound != MAX_ATTEMPTS_BINDING:
                raise ManifestError(
                    f"{where}: loop must bind 'max_attempts' to the "
                    f"caller-supplied {MAX_ATTEMPTS_BINDING!r} (no default)"
                )
        nodes.append(dict(node))
    return nodes


def _check_checks(checks: Any, manifest_id: str) -> list[dict[str, Any]]:
    if not isinstance(checks, list) or not checks:
        raise ManifestError(f"manifest {manifest_id!r}: 'checks' must be a "
                            "non-empty list")
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(checks):
        where = f"manifest {manifest_id!r} check {index}"
        if not isinstance(entry, dict):
            raise ManifestError(f"{where}: must be an object")
        check_id = entry.get("id")
        if not isinstance(check_id, str) or not check_id or check_id in seen:
            raise ManifestError(f"{where}: needs a unique string 'id'")
        seen.add(check_id)
        if not isinstance(entry.get("kind"), str) or not entry["kind"]:
            raise ManifestError(f"{where}: needs a string 'kind'")
        threshold = entry.get("threshold")
        if not isinstance(threshold, dict) or not isinstance(
            threshold.get("source"), str
        ):
            raise ManifestError(
                f"{where}: needs a 'threshold' object with a 'source'"
            )
        out.append(dict(entry))
    return out


def _check_models(payload: Mapping[str, Any], manifest_id: str) -> dict[str, Any]:
    """Validate the ``models`` block: fail-closed bindings and budget shape.

    A role binding may declare ``requires: {open_weight: true}``. Such a
    role must bind the local adapter: binding it to the hosted adapter is
    refused loudly here, before any run, so an open-weight requirement can
    never silently ride on a hosted model. ``models.budget`` is optional at
    load (older manifests omit it and run unbounded), but when present it
    must declare a positive integer ``max_tokens_per_run`` ceiling.
    """
    models = payload.get("models", {})
    if not isinstance(models, dict):
        raise ManifestError(f"manifest {manifest_id!r}: 'models' must be "
                            "an object")
    roles = models.get("roles", {})
    if not isinstance(roles, dict):
        raise ManifestError(f"manifest {manifest_id!r}: 'models.roles' must "
                            "be an object")
    for role, binding in roles.items():
        if not isinstance(binding, Mapping):
            continue
        requires = binding.get("requires", {})
        wants_open = (
            isinstance(requires, Mapping)
            and requires.get("open_weight") is True
        )
        if wants_open:
            adapter = binding.get("adapter", HOSTED_ADAPTER)
            if adapter != LOCAL_ADAPTER:
                raise ManifestError(
                    f"manifest {manifest_id!r}: role {role!r} requires "
                    f"open_weight but is bound to hosted adapter "
                    f"{adapter!r}: refusing (no run)"
                )
    budget = models.get("budget", None)
    if budget is not None:
        if not isinstance(budget, dict):
            raise ManifestError(
                f"manifest {manifest_id!r}: 'models.budget' must be an object"
            )
        ceiling = budget.get("max_tokens_per_run")
        if (
            not isinstance(ceiling, int)
            or isinstance(ceiling, bool)
            or ceiling < 1
        ):
            raise ManifestError(
                f"manifest {manifest_id!r}: 'models.budget' must declare a "
                "positive integer 'max_tokens_per_run'"
            )
    return dict(models)


def validate_manifest(payload: Mapping[str, Any]) -> Manifest:
    """Validate a decoded manifest document; raise ManifestError if invalid."""
    if not isinstance(payload, Mapping):
        raise ManifestError("manifest must be a JSON object")
    manifest_id = _require(payload, "id", "manifest")
    if not isinstance(manifest_id, str) or not manifest_id:
        raise ManifestError("manifest 'id' must be a non-empty string")
    engine_api = _require(payload, "engine_api", f"manifest {manifest_id!r}")
    if engine_api != ENGINE_API:
        raise ManifestError(
            f"manifest {manifest_id!r}: engine_api {engine_api!r} is not "
            f"supported (engine implements {ENGINE_API})"
        )
    source = _require(payload, "source", f"manifest {manifest_id!r}")
    if not isinstance(source, dict):
        raise ManifestError(f"manifest {manifest_id!r}: 'source' must be "
                            "an object")
    raw_capabilities = _require(
        payload, "capabilities", f"manifest {manifest_id!r}"
    )
    if not isinstance(raw_capabilities, dict) or not raw_capabilities:
        raise ManifestError(f"manifest {manifest_id!r}: 'capabilities' must "
                            "be a non-empty object")
    raw_checks = _require(payload, "checks", f"manifest {manifest_id!r}")
    checks = _check_checks(raw_checks, manifest_id)
    check_ids = {entry["id"] for entry in checks}
    capabilities = {
        cap_id: _check_capability(cap_id, spec, check_ids)
        for cap_id, spec in raw_capabilities.items()
    }
    graph = _check_graph(
        _require(payload, "graph", f"manifest {manifest_id!r}"),
        capabilities,
        manifest_id,
    )
    models = _check_models(payload, manifest_id)
    limits = payload.get("limits", {})
    if not isinstance(limits, dict):
        raise ManifestError(f"manifest {manifest_id!r}: 'limits' must be "
                            "an object")
    version = payload.get("version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ManifestError(f"manifest {manifest_id!r}: 'version' must be "
                            "an integer")
    digest = sha256_canonical(dict(payload))
    return Manifest(
        id=manifest_id,
        version=version,
        engine_api=engine_api,
        source=dict(source),
        capabilities=capabilities,
        graph=graph,
        checks=checks,
        models=dict(models),
        limits=dict(limits),
        digest=digest,
    )


def load_manifest(path: str | Path) -> Manifest:
    """Read and validate the manifest at ``path``."""
    manifest_path = Path(path)
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(
            f"manifest {manifest_path} is not valid JSON: {exc}"
        ) from exc
    manifest = validate_manifest(payload)
    return Manifest(
        id=manifest.id,
        version=manifest.version,
        engine_api=manifest.engine_api,
        source=manifest.source,
        capabilities=manifest.capabilities,
        graph=manifest.graph,
        checks=manifest.checks,
        models=manifest.models,
        limits=manifest.limits,
        digest=manifest.digest,
        path=str(manifest_path),
    )


def permission_of(manifest: Manifest, capability_id: str) -> dict[str, Any]:
    """Return the declared permission map for one capability."""
    return {
        "reads_raw_source": bool(
            manifest.capabilities[capability_id].get("reads_raw_source", False)
        ),
        "calls_model": manifest.capabilities[capability_id].get(
            "calls_model"
        ),
        "advisory": bool(
            manifest.capabilities[capability_id].get("advisory", False)
        ),
        "writes_verdict": bool(
            manifest.capabilities[capability_id].get("writes_verdict", False)
        ),
    }

