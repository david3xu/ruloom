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
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..artifacts.hashing import sha256_canonical

#: The engine API version this engine implements. A manifest declaring any
#: other version is rejected rather than run against a mismatched contract.
ENGINE_API = 1

#: The only graph node kinds the engine executes.
NODE_KINDS = ("step", "loop", "gate")

#: Output modalities the engine recognises in a ``generation`` block and
#: in a GenerationSpec. ``scalar`` is the CSV artifact every profile
#: produces; ``imaging`` is the optional synthetic-render branch a
#: profile declares only when it ships image capabilities.
GENERATION_OUTPUTS = ("scalar", "imaging")

#: Release-member source for the engine-rendered evidence report. Like
#: ``engine-log``, the bytes are rendered by the engine at pack time
#: (after the human decision, which the report cites) instead of being
#: read from the frozen snapshot -- the report is a view over the
#: frozen evidence plus the decision, never new evidence itself.
ENGINE_REPORT_SOURCE = "engine-report"

#: The fixed file name of the user-facing evidence summary. Only this
#: name may use the ``engine-report`` source.
ENGINE_REPORT_MEMBER = "evidence-report.md"

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
    release: dict[str, Any] = field(default_factory=dict)
    generation: dict[str, Any] = field(default_factory=dict)


def _require(mapping: Mapping[str, Any], key: str, where: str) -> Any:
    if not isinstance(mapping, Mapping) or key not in mapping:
        raise ManifestError(f"{where}: missing required key {key!r}")
    return mapping[key]


#: Declarations naming how an advisory critic's failure is handled.
#: ``required`` blocks the run; ``optional`` records a visible
#: advisory failure and the run continues on deterministic evidence.
CRITIC_POLICIES = ("required", "optional")


def _check_capability(cap_id: str, spec: Any, check_ids: set[str]) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise ManifestError(f"capability {cap_id!r}: must be an object")
    if not isinstance(spec.get("impl"), str) or not spec["impl"]:
        raise ManifestError(f"capability {cap_id!r}: missing 'impl' reference")
    for key in spec:
        if key in ("impl", "stage", "critic_policy"):
            continue
        if key not in PERMISSION_KEYS:
            raise ManifestError(f"capability {cap_id!r}: unknown permission {key!r}")
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
    policy = spec.get("critic_policy")
    if policy is not None:
        if policy not in CRITIC_POLICIES:
            raise ManifestError(
                f"capability {cap_id!r}: 'critic_policy' must be one of "
                f"{', '.join(CRITIC_POLICIES)}, got {policy!r}"
            )
        if spec.get("advisory") is not True:
            raise ManifestError(
                f"capability {cap_id!r}: 'critic_policy' is declared only "
                "on advisory capabilities"
            )
    _ = check_ids
    return dict(spec)


def _check_graph(
    graph: Any, capabilities: Mapping[str, Any], manifest_id: str
) -> list[dict[str, Any]]:
    if not isinstance(graph, list) or not graph:
        raise ManifestError(
            f"manifest {manifest_id!r}: 'graph' must be a non-empty list"
        )
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
                raise ManifestError(f"{where}: unknown capability {target!r}")
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
                    f"{where}: loop must list a non-empty body of declared capabilities"
                )
            on_fail = node.get("on_fail")
            if not isinstance(on_fail, str) or on_fail not in capabilities:
                raise ManifestError(f"{where}: loop needs an 'on_fail' capability")
            bound = node.get("max_attempts")
            if bound != MAX_ATTEMPTS_BINDING:
                raise ManifestError(
                    f"{where}: loop must bind 'max_attempts' to the "
                    f"caller-supplied {MAX_ATTEMPTS_BINDING!r} (no default)"
                )
        nodes.append(dict(node))
    return nodes


#: Conditions a check entry may name in ``applicable_when``. The engine
#: resolves them against its own run facts before the run decides; an
#: unknown condition is refused at load. Only ``images_present`` exists
#: today (true once image reference statistics are registered).
CHECK_CONDITIONS = ("images_present",)


def _check_checks(checks: Any, manifest_id: str) -> list[dict[str, Any]]:
    if not isinstance(checks, list) or not checks:
        raise ManifestError(
            f"manifest {manifest_id!r}: 'checks' must be a non-empty list"
        )
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
            raise ManifestError(f"{where}: needs a 'threshold' object with a 'source'")
        if "required" in entry and not isinstance(entry["required"], bool):
            raise ManifestError(
                f"{where}: 'required' must be true or false when present"
            )
        condition = entry.get("applicable_when")
        if condition is not None and condition not in CHECK_CONDITIONS:
            raise ManifestError(
                f"{where}: unknown 'applicable_when' {condition!r} "
                f"(known: {', '.join(CHECK_CONDITIONS)})"
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
        raise ManifestError(f"manifest {manifest_id!r}: 'models' must be an object")
    roles = models.get("roles", {})
    if not isinstance(roles, dict):
        raise ManifestError(
            f"manifest {manifest_id!r}: 'models.roles' must be an object"
        )
    for role, binding in roles.items():
        if not isinstance(binding, Mapping):
            continue
        requires = binding.get("requires", {})
        wants_open = (
            isinstance(requires, Mapping) and requires.get("open_weight") is True
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
        if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling < 1:
            raise ManifestError(
                f"manifest {manifest_id!r}: 'models.budget' must declare a "
                "positive integer 'max_tokens_per_run'"
            )
    return dict(models)


#: Where a declared release member's bytes come from. ``frozen`` members
#: are snapshotted before the human prompt and the decision binds their
#: digest; ``engine-log`` members are serialised by the engine itself
#: after the decision (the run log) and never read from working files;
#: ``engine-report`` members are rendered by the engine at pack time
#: from stored records (the evidence report, which cites the decision).
RELEASE_SOURCES = ("frozen", "engine-log", ENGINE_REPORT_SOURCE)

#: The only member name the engine is allowed to regenerate itself.
#: Every other declared member must arrive frozen from the snapshot.
ENGINE_LOG_MEMBER = "run.jsonl"


def _check_release(release: Any, manifest_id: str) -> dict[str, Any]:
    """Validate the optional ``release`` declaration (fail-closed).

    The release block declares the complete release member list, so
    packaging never falls back to a hard-coded file list. Each member
    names a flat file (no directories, no absolute paths, no ``..``),
    whether it is required, an optional applicability condition, and
    where its bytes come from. Unknown keys, duplicate names, and
    unknown conditions or sources are refused at load.
    """
    if release is None:
        return {}
    where = f"manifest {manifest_id!r} 'release'"
    if not isinstance(release, dict):
        raise ManifestError(f"{where} must be an object")
    raw_members = release.get("members", [])
    if not isinstance(raw_members, list) or not raw_members:
        raise ManifestError(f"{where} needs a non-empty 'members' list")
    seen: set[str] = set()
    members: list[dict[str, Any]] = []
    for index, entry in enumerate(raw_members):
        item = f"{where} member {index}"
        if not isinstance(entry, dict):
            raise ManifestError(f"{item}: must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise ManifestError(f"{item}: needs a non-empty string 'name'")
        if (
            name.startswith("/")
            or ".." in name.split("/")
            or "/" in name
            or "\\" in name
        ):
            raise ManifestError(f"{item}: {name!r} must be a flat file name")
        if name in seen:
            raise ManifestError(f"{item}: duplicate member {name!r}")
        seen.add(name)
        required = entry.get("required", True)
        if not isinstance(required, bool):
            raise ManifestError(f"{item}: 'required' must be true or false")
        condition = entry.get("when")
        if condition is not None and condition not in CHECK_CONDITIONS:
            raise ManifestError(
                f"{item}: unknown 'when' {condition!r} "
                f"(known: {', '.join(CHECK_CONDITIONS)})"
            )
        source = entry.get("source", "frozen")
        if source not in RELEASE_SOURCES:
            raise ManifestError(
                f"{item}: unknown 'source' {source!r} "
                f"(known: {', '.join(RELEASE_SOURCES)})"
            )
        if source == "engine-log" and name != ENGINE_LOG_MEMBER:
            raise ManifestError(
                f"{item}: only {ENGINE_LOG_MEMBER!r} may use source 'engine-log'"
            )
        if source == ENGINE_REPORT_SOURCE and name != ENGINE_REPORT_MEMBER:
            raise ManifestError(
                f"{item}: only {ENGINE_REPORT_MEMBER!r} may use "
                f"source {ENGINE_REPORT_SOURCE!r}"
            )
        record: dict[str, Any] = {"name": name, "required": required}
        if condition is not None:
            record["when"] = condition
        if source != "frozen":
            record["source"] = source
        extra = set(entry) - {"name", "required", "when", "source"}
        if extra:
            raise ManifestError(f"{item}: unknown keys {sorted(extra)}")
        members.append(record)
    return {"members": members}


#: Keys of the optional ``generation.request_context`` capability brief.
#: All four are required when the brief is present; nothing else is
#: accepted, so a misspelt or invented key fails closed at load.
REQUEST_CONTEXT_KEYS = (
    "description",
    "output_columns",
    "supported_requests",
    "limitations",
)

#: Upper bound on the brief's ``description`` text (characters).
REQUEST_CONTEXT_MAX_DESCRIPTION = 1500

#: Upper bound on each list entry of the brief (characters).
REQUEST_CONTEXT_MAX_ITEM = 600

#: Upper bound on the number of entries in each list of the brief.
REQUEST_CONTEXT_MAX_ITEMS = 16


def _bounded_text(value: Any, limit: int, where: str) -> str:
    """A non-blank single-line string of at most ``limit`` characters."""
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where} must be a non-empty string")
    if len(value) > limit:
        raise ManifestError(f"{where} exceeds {limit} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ManifestError(f"{where} must not contain control characters")
    return value


def _check_request_context(context: Any, where: str) -> dict[str, Any]:
    """Validate the optional profile-owned capability brief (fail-closed).

    The brief tells request interpretation, in the profile's own words,
    what its implemented generation and checks actually do: a
    ``description`` string plus ``output_columns``, ``supported_requests``
    and ``limitations`` lists. Every field is required once the brief
    is present; each string is non-blank, single-line and bounded; each
    list is non-empty, bounded and free of duplicates. Unknown keys or
    malformed values are refused at load, before any model contact.
    The engine never interprets the wording; it only bounds its shape.
    """
    where = f"{where} 'request_context'"
    if not isinstance(context, dict):
        raise ManifestError(f"{where} must be an object")
    extra = set(context) - set(REQUEST_CONTEXT_KEYS)
    if extra:
        raise ManifestError(f"{where}: unknown keys {sorted(extra)}")
    missing = [key for key in REQUEST_CONTEXT_KEYS if key not in context]
    if missing:
        raise ManifestError(f"{where}: missing required keys {missing}")
    brief: dict[str, Any] = {
        "description": _bounded_text(
            context["description"],
            REQUEST_CONTEXT_MAX_DESCRIPTION,
            f"{where} 'description'",
        )
    }
    for key in REQUEST_CONTEXT_KEYS[1:]:
        items = context[key]
        if (
            not isinstance(items, list)
            or not items
            or len(items) > REQUEST_CONTEXT_MAX_ITEMS
        ):
            raise ManifestError(
                f"{where} {key!r} must be a list of 1 to "
                f"{REQUEST_CONTEXT_MAX_ITEMS} strings"
            )
        checked = [
            _bounded_text(item, REQUEST_CONTEXT_MAX_ITEM, f"{where} {key!r}[{index}]")
            for index, item in enumerate(items)
        ]
        if len(set(checked)) != len(checked):
            raise ManifestError(f"{where} {key!r} contains duplicate entries")
        brief[key] = checked
    return brief


def _check_generation(generation: Any, manifest_id: str) -> dict[str, Any]:
    """Validate the optional ``generation`` declaration (fail-closed).

    The block names the plan strategies the profile implements
    (``strategies``, a non-empty list of unique non-empty strings),
    which of them a default run uses (``default_strategy``, defaulting
    to the first listed), and the output modalities the profile can
    produce (``outputs``, a non-empty subset of ``GENERATION_OUTPUTS``).
    An optional ``request_context`` capability brief describes, for
    request interpretation only, what the implemented generation and
    checks do (see :func:`_check_request_context`); a manifest without
    it keeps working unchanged. A manifest without the block declares
    nothing: no named strategies and scalar output only. Unknown keys,
    an empty strategy list, a default outside the list, an unknown
    output, or a malformed brief are refused at load.
    """
    where = f"manifest {manifest_id!r} 'generation'"
    if generation is None:
        return {}
    if not isinstance(generation, dict):
        raise ManifestError(f"{where} must be an object")
    strategies = generation.get("strategies", [])
    if (
        not isinstance(strategies, list)
        or not strategies
        or any(not isinstance(name, str) or not name for name in strategies)
        or len(set(strategies)) != len(strategies)
    ):
        raise ManifestError(
            f"{where} needs a non-empty 'strategies' list of unique non-empty strings"
        )
    default = generation.get("default_strategy", strategies[0])
    if default not in strategies:
        raise ManifestError(
            f"{where}: 'default_strategy' {default!r} is not one of "
            f"the declared strategies {list(strategies)!r}"
        )
    outputs = generation.get("outputs", ["scalar"])
    if (
        not isinstance(outputs, list)
        or not outputs
        or any(output not in GENERATION_OUTPUTS for output in outputs)
    ):
        raise ManifestError(
            f"{where}: 'outputs' must be a non-empty list drawn from "
            f"{list(GENERATION_OUTPUTS)!r}, got {outputs!r}"
        )
    extra = set(generation) - {
        "strategies",
        "default_strategy",
        "outputs",
        "request_context",
    }
    if extra:
        raise ManifestError(f"{where}: unknown keys {sorted(extra)}")
    checked: dict[str, Any] = {
        "strategies": list(strategies),
        "default_strategy": default,
        "outputs": list(outputs),
    }
    if "request_context" in generation:
        checked["request_context"] = _check_request_context(
            generation["request_context"], where
        )
    return checked


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
        raise ManifestError(f"manifest {manifest_id!r}: 'source' must be an object")
    raw_capabilities = _require(payload, "capabilities", f"manifest {manifest_id!r}")
    if not isinstance(raw_capabilities, dict) or not raw_capabilities:
        raise ManifestError(
            f"manifest {manifest_id!r}: 'capabilities' must be a non-empty object"
        )
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
    release = _check_release(payload.get("release"), manifest_id)
    generation = _check_generation(payload.get("generation"), manifest_id)
    limits = payload.get("limits", {})
    if not isinstance(limits, dict):
        raise ManifestError(f"manifest {manifest_id!r}: 'limits' must be an object")
    version = payload.get("version", 0)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ManifestError(f"manifest {manifest_id!r}: 'version' must be an integer")
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
        release=dict(release),
        generation=dict(generation),
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
        release=dict(manifest.release),
        generation=dict(manifest.generation),
    )


def permission_of(manifest: Manifest, capability_id: str) -> dict[str, Any]:
    """Return the declared permission map for one capability."""
    return {
        "reads_raw_source": bool(
            manifest.capabilities[capability_id].get("reads_raw_source", False)
        ),
        "calls_model": manifest.capabilities[capability_id].get("calls_model"),
        "advisory": bool(manifest.capabilities[capability_id].get("advisory", False)),
        "writes_verdict": bool(
            manifest.capabilities[capability_id].get("writes_verdict", False)
        ),
    }
