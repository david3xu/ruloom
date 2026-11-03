"""Graph execution with mechanical permission enforcement.

The runner walks the manifest graph, which uses only three node kinds:

* ``step`` -- run one capability;
* ``loop`` -- run a body of capabilities, invoking ``on_fail`` between
  attempts, bounded by the caller-supplied ``max_attempts`` (no default);
* ``gate`` -- stop for a human decision recorded verbatim.

The engine privately owns verdicts, chosen artifacts, and state
transitions. Each capability receives a :class:`RunContext`: a frozen
view of its declared inputs (statistics views, prior attempt records)
plus narrow engine-mediated write methods with validated return values.
Capabilities get no reference to the runner, no mutable shared state
dict, and no path to verdict, artifact-selection, or decision state.
Advisory outputs go to a separate advisory store that verdict
computation never reads. Raw source bytes, source paths, and parsed
source frames are handed only to capabilities declaring
``reads_raw_source``. A model client is handed only to capabilities
declaring a ``calls_model`` role, and only when that role resolves to an
enabled binding. Only capabilities declaring ``writes_verdict`` may
record check results, and every recorded check must be declared in the
manifest with a recognised status; the engine binds the artifact hash,
the attempt, and the verifier itself, and computes the verdict only from
that validated evidence.

Trust assumption (in-process typing is an accidental-misuse boundary,
not a sandbox): these guarantees stop a well-meaning capability from
reaching authority it was not granted through the declared interface.
Arbitrary third-party plugins are not sandboxed here; running untrusted
code would need isolated processes with restricted input mounts and
mediated model/network access.

Loop policy (engine-owned): keep the best attempt by check score, stop on
the bound or on no improvement across two consecutive attempts. Exhaustion
reports the failing checks and never ships as passing.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..audit.runlog import RunLog
from .manifest import (
    ENGINE_REPORT_MEMBER,
    ENGINE_REPORT_SOURCE,
    HOSTED_ADAPTER,
    LOCAL_ADAPTER,
    MAX_ATTEMPTS_BINDING,
    Manifest,
    permission_of,
)
from .records import (
    VALID_CHECK_STATUSES,
    Artifact,
    Attempt,
    Decision,
    EngineCheckResult,
    Run,
)
from .runstore import RunStore, record_refusal
from .verdict import (
    CONDITION_IMAGES_PRESENT,
    applicable_required_ids,
    decide,
    unconditional_required_ids,
)

#: Status values the runner assigns to loop attempts.
ATTEMPT_PASS = "pass"
ATTEMPT_FAIL = "fail"

#: Exit codes mirroring the command contract: released, failed, declined.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_DECLINED = 2


class PermissionDenied(Exception):
    """A capability reached for something its manifest forbids."""


class RunnerError(Exception):
    """The graph, registry, or capability outputs do not line up."""


class BudgetExceeded(RunnerError):
    """The run's model token ceiling was reached before the next call.

    Raised before the next model call is made (never mid-call), so the
    offending call never happens and the run fails loudly instead of
    silently overspending.
    """


#: A capability implementation: profile code receiving a scoped context.
CapabilityFn = Callable[
    [
        "RunContext",
    ],
    None,
]

#: Builds a model client for a resolved role configuration.
ModelFactory = Callable[[str, Mapping[str, Any]], Any]


@dataclass(frozen=True)
class _Permissions:
    reads_raw_source: bool = False
    calls_model: str | None = None
    advisory: bool = False
    writes_verdict: bool = False


def usage_tokens(usage: Any) -> int:
    """Count the budget tokens one model call reported.

    The budget unit is ``input + output + reasoning`` tokens as the
    adapter reports them (local calls count the same way; their cost is
    zero but the budget is denominated in tokens, not money). Missing or
    non-integer fields count as zero.
    """
    total = 0
    for field in ("input_tokens", "output_tokens", "reasoning_tokens"):
        value = getattr(usage, field, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(usage, Mapping):
                raw = usage.get(field, 0)
                if isinstance(raw, bool) or not isinstance(raw, int):
                    continue
                total += raw
            continue
        total += value
    return total


class _BudgetedClient:
    """A model client wrapper that enforces the run token budget.

    The ceiling is checked before every call: once cumulative usage
    reaches ``budget`` the next call raises :class:`BudgetExceeded`
    without touching the wrapped client. Usage reported by each allowed
    call accumulates on the runner, and the call's effective invocation
    (argv, pinned cwd, inherited env key names, contributor flag, and
    the returned model identity) is recorded to the run log, so the
    run record shows exactly which restricted invocation acted.
    """

    def __init__(self, client: Any, runner: _Runner, role: str) -> None:
        self._client = client
        self._runner = runner
        self._role = role

    def _check_budget(self) -> None:
        self._runner.check_model_policy(self._role)
        budget = self._runner.model_budget
        if budget is not None and self._runner.model_tokens >= budget:
            raise BudgetExceeded(
                f"model token budget exceeded: {self._runner.model_tokens} "
                f"tokens used of {budget} allowed; refusing the next model "
                "call before making it"
            )

    def _complete(
        self, method: str, prompt: str, schema: Mapping[str, Any], *args: Any
    ) -> Any:
        self._check_budget()
        self._runner.log.append("model_call_started", "started", {"role": self._role})
        try:
            completion = getattr(self._client, method)(prompt, schema, *args)
        except Exception as exc:
            self._runner.log.append(
                "model_call_failed",
                "error",
                {
                    "role": self._role,
                    "error": f"{type(exc).__name__}: {exc}",
                    "returned_model": "unknown",
                },
            )
            raise
        self._runner.record_model_usage(completion.usage)
        self._runner.note_model_call(completion.usage)
        self._runner.note_model_invocation(self._role, self._client, completion)
        return completion

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Any:
        return self._complete("complete", prompt, schema)

    def complete_with_image(
        self, prompt: str, schema: Mapping[str, Any], image_path: Any
    ) -> Any:
        return self._complete("complete_with_image", prompt, schema, image_path)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_client"], name)


def _copy(value: Any) -> Any:
    """An immutable-by-convention snapshot of engine-owned data."""
    return copy.deepcopy(value)


def _must_json(value: Any, what: str, capability_id: str) -> Any:
    """Deep-copy ``value``, refusing it when it is not JSON-native."""
    try:
        json.dumps(value)
    except (TypeError, ValueError) as exc:
        raise RunnerError(
            f"capability {capability_id!r} supplied a non-JSON {what}: {exc}"
        ) from exc
    return copy.deepcopy(value)


class RunContext:
    """The scoped view one capability execution receives.

    Frozen inputs plus validated writes. The context carries immutable
    snapshots of the capability's declared inputs -- statistics views,
    prior attempt records, the selected artifact identity -- and narrow
    engine-mediated methods that validate and store declared outputs.
    Profiles never touch the runner, the run log, other capabilities, or
    any mutable shared state: this object exposes no ``state`` dict, no
    runner reference, no manifest object, and no path to verdict,
    artifact-selection, or decision state. Those stay private to the
    engine, which computes them only from validated check results.
    """

    def __init__(
        self,
        *,
        capability_id: str,
        permissions: _Permissions,
        engine: _Runner,
    ) -> None:
        self.capability_id = capability_id
        self.permissions = permissions
        self._engine = engine

    def _deny_unless_raw(self, what: str) -> None:
        if not self.permissions.reads_raw_source:
            raise PermissionDenied(
                f"capability {self.capability_id!r} does not declare "
                f"reads_raw_source: {what} is not materialised for it"
            )

    @property
    def attempt_number(self) -> int:
        """1-based attempt inside a loop body; 0 outside any loop."""
        return self._engine.loop_attempt_no

    @property
    def declared_checks(self) -> tuple[dict[str, Any], ...]:
        """Immutable copies of the manifest check declarations."""
        return tuple(
            copy.deepcopy(dict(entry)) for entry in self._engine.manifest.checks
        )

    @property
    def source_config(self) -> dict[str, Any]:
        """Immutable copy of the manifest ``source`` configuration."""
        return copy.deepcopy(dict(self._engine.manifest.source))

    @property
    def source_path(self) -> Path:
        """Filesystem path of the profile source (raw-granted only)."""
        self._deny_unless_raw("the source path")
        return self._engine.source

    @property
    def images_dir(self) -> Path | None:
        """Optional directory of companion source files (raw-granted only)."""
        self._deny_unless_raw("the companion directory")
        return self._engine.images_dir

    @property
    def local_critic_enabled(self) -> bool:
        """Whether the optional local text-critique step should run."""
        return self._engine.local_critic_enabled

    @property
    def out_dir(self) -> Path:
        """Base output directory for run artifacts."""
        return self._engine.out

    def raw_bytes(self) -> bytes:
        """Return the raw source bytes (guarded by ``reads_raw_source``)."""
        self._deny_unless_raw("raw source bytes")
        return self._engine.source_bytes

    def model(self, role: str) -> Any:
        """Return the model client for ``role`` (guarded by ``calls_model``)."""
        granted = self.permissions.calls_model
        if granted is None or granted != role:
            raise PermissionDenied(
                f"capability {self.capability_id!r} is not granted model "
                f"role {role!r}: model client withheld"
            )
        return self._engine.model_for(role)

    def source_meta(self) -> dict[str, Any]:
        """Immutable copy of the recorded source identity record."""
        return _copy(self._engine.source_meta_record())

    def set_source_meta(
        self,
        *,
        name: str,
        sha256: str,
        licence: Any,
        attribution: Any,
        manifest_name: Any = None,
        manifest_sha256: Any = None,
        hash_verified: Any = False,
        output_terms_grant: Any = None,
    ) -> None:
        """Record the source identity and rights evidence measured at intake.

        ``manifest_name``/``manifest_sha256`` identify the sibling registry
        record the source was checked against, and ``hash_verified`` says
        whether the measured source hash matched that record.
        ``output_terms_grant`` carries the explicit output-terms grant
        recorded in that sibling manifest (if any): licence presence
        alone is never a grant. Unverified sources are stored as-is: the
        engine renders them with no output-rights clearance instead of
        refusing them here.
        """
        if not isinstance(name, str) or not name:
            raise RunnerError("source name must be a non-empty string")
        if not isinstance(sha256, str) or not sha256:
            raise RunnerError("source sha256 must be a non-empty string")
        self._engine.store_source_meta(
            {
                "source_name": name,
                "source_sha256": sha256,
                "licence": licence,
                "attribution": attribution,
                "manifest_name": manifest_name,
                "manifest_sha256": manifest_sha256,
                "hash_verified": hash_verified is True,
                "output_terms_grant": output_terms_grant,
            }
        )

    def source_frame(self) -> Any:
        """Immutable copy of the parsed source frame (raw-granted only)."""
        self._deny_unless_raw("the parsed source frame")
        return self._engine.read_source_frame()

    def set_source_frame(self, frame: Any) -> None:
        """Store the parsed source frame for granted downstream readers."""
        self._deny_unless_raw("the parsed source frame")
        self._engine.store_source_frame(frame)

    def profile(self) -> Any:
        """Immutable copy of the statistics profile."""
        return self._engine.read_profile()

    def set_profile(self, profile: Any) -> None:
        """Store the statistics profile built from the source frame."""
        self._engine.store_profile(profile)

    def plan(self) -> dict[str, Any]:
        """Immutable copy of the current generation plan."""
        return self._engine.read_plan()

    def set_plan(self, plan: Mapping[str, Any]) -> None:
        """Store a generation plan (must be a JSON-native mapping)."""
        if not isinstance(plan, Mapping):
            raise RunnerError(
                f"capability {self.capability_id!r} supplied a plan that "
                "is not a mapping"
            )
        self._engine.store_plan(_must_json(dict(plan), "plan", self.capability_id))

    def plan_bounds(self) -> dict[str, Any]:
        """Immutable copy of the manifest limits bounding plans."""
        return self._engine.plan_bounds()

    def spec_record(self) -> dict[str, Any]:
        """Immutable copy of the normalised GenerationSpec for this run."""
        return self._engine.spec_record()

    def spec_digest(self) -> str:
        """Canonical SHA-256 of the normalised GenerationSpec."""
        return self._engine.spec_digest_value()

    def fixed_plan(self) -> dict[str, Any] | None:
        """The caller-supplied fixed plan for replay, if any.

        Profiles read this in their planning step: when a fixed plan
        is present, the capability validates and stores it instead of
        requesting a new plan, so no planner call is made.
        """
        return self._engine.fixed_plan_record()

    def selected_plan(self) -> dict[str, Any]:
        """Immutable copy of the engine-selected attempt's plan.

        Packaging must write this plan (and its seed) with the
        selected artifact: the current plan may belong to a later,
        worse attempt, while the selected record is the whole attempt
        the engine chose.
        """
        return self._engine.selected_plan_record()

    def reference_stats(self) -> dict[str, Any]:
        """Immutable copy of the source-derived reference statistics."""
        return _copy(self._engine.reference_stats_record())

    def set_reference_stats(self, reference: Mapping[str, Any]) -> None:
        """Store source-derived reference statistics for the checks."""
        if not isinstance(reference, Mapping):
            raise RunnerError(
                f"capability {self.capability_id!r} supplied reference "
                "statistics that are not a mapping"
            )
        self._engine.store_reference_stats(
            _must_json(dict(reference), "reference statistics", self.capability_id)
        )

    def companion(self) -> dict[str, Any]:
        """Immutable copy of the companion (image) bundle, if any."""
        return _copy(self._engine.companion_record())

    def set_companion(self, bundle: Mapping[str, Any]) -> None:
        """Store the companion (image) bundle for downstream steps."""
        if not isinstance(bundle, Mapping):
            raise RunnerError(
                f"capability {self.capability_id!r} supplied a companion "
                "bundle that is not a mapping"
            )
        self._engine.store_companion(
            _must_json(dict(bundle), "companion bundle", self.capability_id)
        )

    def latest_report(self) -> Any:
        """The most recently recorded validation report."""
        return self._engine.read_latest_report()

    def report_for(self, sha256: str) -> Any:
        """The validation report recorded for one artifact hash."""
        return self._engine.read_report_for(sha256)

    def set_report(self, report: Any) -> None:
        """Store a validation report bound to the current artifact."""
        self._engine.store_report(report, self.capability_id)

    def prior_attempts(self) -> tuple[dict[str, Any], ...]:
        """Immutable records of completed attempts, oldest first."""
        return self._engine.prior_attempt_records()

    def selected_artifact_sha(self) -> str | None:
        """The engine-selected artifact hash, once a loop has decided."""
        return self._engine.selected_artifact_record()

    def artifact_dir(self) -> str | None:
        """The engine-registered artifact directory, once prepared."""
        return self._engine.artifact_dir_record()

    def record_checks(self, items: list[Mapping[str, Any]]) -> None:
        """Record verdict checks (guarded by ``writes_verdict``).

        Each item declares ``check_id``, ``kind``, ``status`` (one of
        ``pass``, ``fail``, ``error``, ``unsupported``, ``skipped`` --
        only ``pass`` satisfies a required check), ``measured``,
        ``threshold`` and ``threshold_source``. An item may name
        ``artifact_sha256`` explicitly; otherwise the engine binds the
        latest artifact of the current phase. A hash that does not match
        the attempt artifact is rejected as invalid. The engine also
        binds the attempt number and the calling capability as verifier;
        undeclared check ids, kind mismatches, and unrecognised statuses
        are rejected. Recording never decides: the engine computes the
        verdict from the validated evidence.
        """
        if not self.permissions.writes_verdict:
            raise PermissionDenied(
                f"capability {self.capability_id!r} does not declare "
                "writes_verdict: it cannot produce check results"
            )
        if not isinstance(items, list):
            raise RunnerError("check items must be provided as a list")
        self._engine.store_checks(
            self.capability_id, items, self._engine.loop_attempt_no
        )

    def _check_item(
        self, item: Any, *, artifact_sha256: str, attempt: int
    ) -> EngineCheckResult:
        if not isinstance(item, Mapping):
            raise RunnerError("check items must be mappings")
        check_id = item.get("check_id")
        declared = {str(entry["id"]): entry for entry in self.declared_checks}
        entry = declared.get(str(check_id)) if isinstance(check_id, str) else None
        if entry is None:
            raise RunnerError(
                f"capability {self.capability_id!r} recorded undeclared "
                f"check {check_id!r}"
            )
        for key in ("kind", "status", "threshold_source"):
            if key not in item:
                raise RunnerError(f"check {check_id!r} is missing {key!r}")
        if item["kind"] != entry.get("kind"):
            raise RunnerError(
                f"check {check_id!r} kind {item['kind']!r} does not match "
                f"declared kind {entry.get('kind')!r}"
            )
        status = item["status"]
        if status not in VALID_CHECK_STATUSES:
            raise RunnerError(
                f"check {check_id!r} status {status!r} is not recognised "
                f"(expected one of {', '.join(VALID_CHECK_STATUSES)})"
            )
        position = item.get("band_position")
        if position is not None and (
            isinstance(position, bool) or not isinstance(position, (int, float))
        ):
            raise RunnerError(f"check {check_id!r}: 'band_position' must be a number")
        return EngineCheckResult(
            check_id=str(check_id),
            kind=str(item["kind"]),
            status=str(status),
            measured=item.get("measured"),
            threshold=item.get("threshold"),
            threshold_source=str(item["threshold_source"]),
            artifact_sha256=artifact_sha256,
            verifier=self.capability_id,
            attempt=attempt,
            detail=str(item.get("detail", "")),
            band_position=position,
        )

    def record_advisory(self, name: str, payload: Any) -> None:
        """Log an advisory output; it never affects pass/fail.

        Advisories land in a separate advisory store. Verdict computation
        never reads that store, so advisory output cannot flip a verdict.
        """
        self._engine.store_advisory(self.capability_id, name, payload)
        self.event(
            f"{self.capability_id}:advisory",
            "ok",
            {"name": name, "payload": payload},
        )

    def advisory(self, name: str) -> Any:
        """Immutable copy of one recorded advisory payload, if any."""
        return self._engine.read_advisory(self.capability_id, name)

    def record_artifact(self, name: str, media_type: str, payload: bytes) -> Artifact:
        """Store one byte payload; identity is its SHA-256."""
        return self._engine.store_artifact(
            name, media_type, payload, self.capability_id
        )

    def event(
        self,
        stage: str,
        status: str,
        details: Mapping[str, Any] | None = None,
        usage: Any = None,
        artifact_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Append one event to the append-only run log."""
        return self._engine.log.append(
            stage,
            status,
            dict(details or {}),
            usage=usage,
            artifact_sha256=artifact_sha256,
        )

    def flush_log(self, path: str | Path) -> Path:
        """Write the run log to ``path`` as JSON lines."""
        return self._engine.log.write_jsonl(path)

    def artifact_payload(self, sha256: str) -> bytes:
        """Return stored bytes for one recorded artifact hash."""
        try:
            return self._engine.artifact_payloads[sha256]
        except KeyError as exc:
            raise RunnerError(f"no stored payload for artifact {sha256}") from exc

    def latest_artifact(self) -> Artifact:
        """The record for the most recently stored artifact."""
        return self._engine.latest_artifact_record()

    def prepare_artifact_dir(self) -> Path:
        """Create and register the directory for the selected artifact."""
        return self._engine.prepare_artifact_dir(self.capability_id)

    def provenance_text(self) -> str:
        """Render the release provenance statement from engine state."""
        return self._engine.provenance_record()

    def pack_release(self) -> tuple[Path, str]:
        """Write the reproducible release ZIP for the artifact directory."""
        return self._engine.pack_recorded_release(self.capability_id)

    def say(self, message: str) -> None:
        """Print one line of run progress."""
        self._engine.say(message)


class _Runner:
    """Internal execution state for one profile run."""

    def __init__(
        self,
        *,
        manifest: Manifest,
        registry: Mapping[str, CapabilityFn],
        source: Path,
        max_attempts: int,
        model_factory: ModelFactory,
        out: Path,
        say: Callable[[str], None],
        ask: Callable[[str], str],
        images_dir: Path | None = None,
        local_critic: bool = False,
        role_bindings: Mapping[str, Mapping[str, Any]] | None = None,
        role_binding_sources: Mapping[str, Mapping[str, str]] | None = None,
        spec: Mapping[str, Any] | None = None,
        spec_digest: str | None = None,
        fixed_plan: Mapping[str, Any] | None = None,
        model_policy: str = "hosted",
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError(f"max_attempts must be an integer, got {max_attempts!r}")
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")
        missing = [c for c in manifest.capabilities if c not in registry]
        if missing:
            raise RunnerError(f"registry is missing capabilities: {', '.join(missing)}")
        from .spec import normalize_spec as _normalize

        if spec is None:
            spec, spec_digest = _normalize(
                None,
                manifest,
                source=source,
                max_attempts=max_attempts,
                images_dir=images_dir,
            )
        else:
            spec = dict(spec)
            raw_limits = spec.get("limits")
            if isinstance(raw_limits, Mapping):
                spec_attempts = raw_limits.get("max_attempts", max_attempts)
                if (
                    isinstance(spec_attempts, bool)
                    or not isinstance(spec_attempts, int)
                    or spec_attempts < 1
                ):
                    raise RunnerError(
                        "run spec 'limits.max_attempts' must be a positive "
                        f"integer, got {spec_attempts!r}: refusing (no run)"
                    )
                if spec_attempts != max_attempts:
                    raise RunnerError(
                        f"run spec max_attempts {spec_attempts} contradicts "
                        f"the caller bound {max_attempts}: refusing (no run)"
                    )
            if spec_digest is None:
                from ..artifacts.hashing import sha256_canonical

                spec_digest = sha256_canonical(spec)
        self._spec = spec
        from .model_policy import validate_policy

        validate_policy(model_policy)
        self.model_policy = model_policy
        self._spec_digest = spec_digest
        self._fixed_plan: dict[str, Any] | None = (
            copy.deepcopy(dict(fixed_plan)) if isinstance(fixed_plan, Mapping) else None
        )
        self.manifest = manifest
        self.registry = dict(registry)
        self.source = source
        if images_dir is not None:
            images_dir = Path(images_dir)
            if not images_dir.is_dir():
                raise RunnerError(f"images directory not found: {images_dir}")
        self.images_dir = images_dir
        self.local_critic_enabled = bool(local_critic)
        self.max_attempts = max_attempts
        self.model_factory = model_factory
        self.out = out
        self._say = say
        self._ask = ask
        self.log = RunLog()
        self._in_loop = False
        # Engine-private run state. Capabilities never see this object;
        # they exchange frozen copies and validated writes through
        # RunContext, and the engine alone derives verdicts, the selected
        # artifact, and the decision from the validated evidence.
        self._source_meta: dict[str, Any] = {}
        self._source_frame: Any = None
        self._profile: Any = None
        self._plan: dict[str, Any] | None = None
        self._reports: dict[str, Any] = {}
        self._latest_report_sha: str | None = None
        self._reference_stats: dict[str, Any] = {}
        self._companion: dict[str, Any] = {}
        self._best: Attempt | None = None
        self._selected_artifact_sha: str | None = None
        self._artifact_dir: str | None = None
        self._decision: dict[str, Any] | None = None
        self._loop_verdict: str | None = None
        self._loop_failed: list[str] = []
        self._final_verdict: str | None = None
        self._final_failed: list[str] = []
        # Frozen release snapshot: member bytes captured when the run
        # reaches the human gate, the candidate entries describing them,
        # and the digest the human decision binds. Packaging reads only
        # these bytes, never mutable working files.
        self._frozen_files: dict[str, bytes] | None = None
        self._frozen_entries: list[dict[str, Any]] | None = None
        self._frozen_attempt: int | None = None
        self._evidence_digest: str | None = None
        self._last_usage: Any = None
        self._post_artifacts: list[str] = []
        self.pending_checks: list[EngineCheckResult] = []
        self.advisories: list[dict[str, Any]] = []
        self.attempts: list[Attempt] = []
        self.artifact_records: list[Artifact] = []
        self.artifact_payloads: dict[str, bytes] = {}
        self.current_artifact_sha: str | None = None
        self.attempt_artifacts: list[str] = []
        declared = manifest.models.get("roles", {})
        if not isinstance(declared, dict):
            declared = {}
        self.role_bindings: dict[str, dict[str, Any]] = {
            role: dict(config) if isinstance(config, Mapping) else {}
            for role, config in (
                role_bindings.items() if role_bindings is not None else declared.items()
            )
        }
        source_map = role_binding_sources if role_binding_sources is not None else {}
        self.role_binding_sources: dict[str, dict[str, str]] = {
            role: dict(sources) for role, sources in source_map.items()
        }
        self._refuse_unresolvable_roles()
        budget = manifest.models.get("budget", None)
        ceiling = None
        if isinstance(budget, Mapping):
            raw = budget.get("max_tokens_per_run")
            if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
                ceiling = raw
        self.model_budget: int | None = ceiling
        self.model_tokens = 0
        self.run = Run(
            id=self.log.run_id,
            profile_ref=manifest.id,
            profile_version=manifest.version,
            profile_digest=manifest.digest,
            request={
                "source": str(source),
                "max_attempts": max_attempts,
                "out": str(out),
                **({"images_dir": str(images_dir)} if images_dir is not None else {}),
                **({"local_critic": True} if local_critic else {}),
                "spec": dict(self._spec),
                "spec_digest": self._spec_digest,
                "model_policy": self.model_policy,
                **(
                    {"fixed_plan": copy.deepcopy(dict(self._fixed_plan))}
                    if self._fixed_plan is not None
                    else {}
                ),
            },
            limits=dict(manifest.limits),
        )
        # The durable history exists before intake runs: the directory
        # is created here and every later log event is persisted
        # write-through, so even a failure in the first capability
        # leaves a durable failed run record instead of an in-memory id.
        self.store = RunStore(out, self.run)
        self.log.set_sink(self.store.append_event)
        try:
            self.source_bytes = source.read_bytes()
        except OSError as exc:
            failure = f"cannot read source {source}: {exc}"
            self.log.append("source_intake", "error", {"error": failure})
            self.store.update_run("error", error=failure)
            raise RunnerError(failure) from exc
        self._models: dict[str, Any] = {}

    def say(self, message: str) -> None:
        self._say(message)

    @property
    def loop_attempt_no(self) -> int:
        """1-based attempt number inside a loop body; 0 outside."""
        return len(self.attempts) + 1 if self._in_loop else 0

    def _refuse_unresolvable_roles(self) -> None:
        """Refuse unresolvable role bindings at composition time.

        Every capability that declares ``calls_model`` must resolve to a
        bound, enabled role, and an open-weight requirement must never sit
        on the hosted adapter. This runs in the constructor so a bad
        binding fails before any capability -- and before any model call.
        """
        for capability_id, spec in self.manifest.capabilities.items():
            role = spec.get("calls_model")
            if role is None:
                continue
            if role not in self.role_bindings:
                raise RunnerError(
                    f"capability {capability_id!r} calls model role "
                    f"{role!r}, which is not bound: refusing (no run)"
                )
            config = self.role_bindings[role]
            if config.get("enabled", True) is False:
                raise RunnerError(
                    f"capability {capability_id!r} calls model role "
                    f"{role!r}, which is disabled: refusing (no run)"
                )
            requires = config.get("requires", {})
            if (
                isinstance(requires, Mapping)
                and requires.get("open_weight") is True
                and config.get("adapter", HOSTED_ADAPTER) != LOCAL_ADAPTER
            ):
                raise RunnerError(
                    f"capability {capability_id!r} calls model role "
                    f"{role!r}, which requires open_weight but is bound "
                    "to the hosted adapter: refusing (no run)"
                )

    def record_model_usage(self, usage: Any) -> None:
        """Accumulate one model call's reported tokens toward the budget."""
        self.model_tokens += usage_tokens(usage)

    def note_model_call(self, usage: Any) -> None:
        """Remember the latest model usage for the attempt log entry."""
        self._last_usage = usage

    def note_model_invocation(self, role: str, client: Any, completion: Any) -> None:
        """Record one model call's effective invocation to the run log.

        The hosted adapter exposes its restricted invocation (argv,
        pinned cwd, inherited env key names, contributor flag, the
        requested model, and the observed model the transport reported);
        any other client records its type plus the requested and observed
        identities read from the completion and the client. Requested vs
        observed stay separate: ``model`` names what was asked,
        ``returned_model`` names what the transport reported (``unknown``
        when it reported none), and ``models`` carries the normalised
        per-model entries when the completion provides them (a
        ``multiple:...`` observed name aggregates several reported
        identities). A mismatch never refuses; both sides are recorded.
        The same split covers permissions: ``requested_permissions``
        names the policy asked for while ``observed_permissions`` names
        the effective permissions the spawn provided, including the
        confinement limit it does not provide. Only key names are
        stored -- secret values never enter the record -- and the event
        rides the write-through sink, so the invocation is durable per
        call.
        """
        details: dict[str, Any] = {
            "role": role,
            "client": type(client).__name__,
            "adapter": self.role_bindings.get(role, {}).get("adapter", HOSTED_ADAPTER),
        }
        meta = getattr(completion, "meta", None)
        meta_models: Any = meta.get("models") if isinstance(meta, Mapping) else None
        models_copy = (
            [dict(entry) for entry in meta_models if isinstance(entry, Mapping)]
            if isinstance(meta_models, list)
            else None
        )
        invocation = getattr(client, "last_invocation", None)
        if isinstance(invocation, Mapping):
            requested = invocation.get("model", "")
            if not isinstance(requested, str) or not requested.strip():
                requested = ""
            returned = invocation.get("returned_model", invocation.get("model", ""))
            if not isinstance(returned, str) or not returned.strip():
                returned = "unknown"
            record: dict[str, Any] = {
                "argv": list(invocation.get("argv", [])),
                "cwd": str(invocation.get("cwd", "")),
                "env_keys": sorted(str(key) for key in invocation.get("env_keys", [])),
                "contributor": bool(invocation.get("contributor", False)),
                "model": requested,
                "returned_model": returned,
            }
            requested_perms = invocation.get("requested_permissions")
            if isinstance(requested_perms, Mapping):
                record["requested_permissions"] = dict(requested_perms)
            observed_perms = invocation.get("observed_permissions")
            if isinstance(observed_perms, Mapping):
                record["observed_permissions"] = dict(observed_perms)
            if models_copy is not None:
                record["models"] = models_copy
            details["invocation"] = record
        else:
            usage = getattr(completion, "usage", None)
            usage_model = getattr(usage, "model", "")
            meta_model = meta.get("model", "") if isinstance(meta, Mapping) else ""
            meta_requested = (
                meta.get("requested_model", "") if isinstance(meta, Mapping) else ""
            )
            client_model = getattr(client, "model", "")
            observed = self._observed_name(usage_model, meta_model)
            requested = self._observed_name(
                meta_requested,
                client_model,
                meta_model,
                usage_model,
                default="",
            )
            if not requested:
                requested = observed
            details["model"] = requested
            details["returned_model"] = observed
            if models_copy is not None:
                details["models"] = models_copy
        self.log.append(
            "model_invocation", "ok", details, usage=getattr(completion, "usage", None)
        )

    @staticmethod
    def _observed_name(*candidates: Any, default: str = "unknown") -> str:
        """First usable identity among ``candidates``, else ``default``."""
        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate
        return default

    def source_meta_record(self) -> dict[str, Any]:
        """The recorded source identity record (possibly empty)."""
        return self._source_meta

    def store_source_meta(self, meta: Mapping[str, Any]) -> None:
        """Store the source identity measured at intake."""
        self._source_meta = dict(meta)

    def read_source_frame(self) -> Any:
        """The parsed source frame; loud when intake never stored one."""
        if self._source_frame is None:
            raise RunnerError("no source frame recorded yet")
        return copy.deepcopy(self._source_frame)

    def store_source_frame(self, frame: Any) -> None:
        """Store the parsed source frame for granted downstream readers."""
        if not isinstance(frame, (list, dict)):
            raise RunnerError(
                f"source frame must be a list or a mapping, got {type(frame).__name__}"
            )
        self._source_frame = copy.deepcopy(frame)

    def read_profile(self) -> Any:
        """The statistics profile; loud when profiling never stored one."""
        if self._profile is None:
            raise RunnerError("no statistics profile recorded yet")
        return copy.deepcopy(self._profile)

    def store_profile(self, profile: Any) -> None:
        """Store the statistics profile built from the source frame."""
        if profile is None:
            raise RunnerError("cannot store an empty statistics profile")
        self._profile = copy.deepcopy(profile)

    def read_plan(self) -> dict[str, Any]:
        """The current generation plan; loud when no plan was stored."""
        if self._plan is None:
            raise RunnerError("no generation plan recorded yet")
        return copy.deepcopy(self._plan)

    def plan_bounds(self) -> dict[str, Any]:
        """Immutable copy of the manifest limits bounding plans.

        Profiles read this to give the plan schema its explicit
        maximum (for example ``max_rows`` bounding ``n_rows``); the
        engine enforces the same bounds authoritatively in
        :meth:`store_plan` before any generation or check runs.
        """
        return copy.deepcopy(dict(self.manifest.limits))

    def spec_record(self) -> dict[str, Any]:
        """Immutable copy of the normalised GenerationSpec."""
        return copy.deepcopy(dict(self._spec))

    def spec_digest_value(self) -> str:
        """Canonical SHA-256 of the normalised GenerationSpec."""
        return str(self._spec_digest)

    def fixed_plan_record(self) -> dict[str, Any] | None:
        """Immutable copy of the caller-supplied fixed plan, if any."""
        if self._fixed_plan is None:
            return None
        return copy.deepcopy(dict(self._fixed_plan))

    def _enforce_plan_bounds(self, plan: Mapping[str, Any]) -> None:
        """Refuse an over-limit plan before generation or checks run.

        Every manifest ``max_<field>`` limit bounds the plan's
        ``<field>`` entry, with ``max_rows`` bounding the plan's
        ``n_rows`` entry. An oversize plan is refused loudly here -- at
        plan-store time, so the generator never starts -- and the
        refusal propagates to a durable failed run record through the
        normal error path.
        """
        limits = self.manifest.limits
        if not isinstance(limits, Mapping):
            return
        for key, ceiling in limits.items():
            if not isinstance(key, str) or not key.startswith("max_"):
                continue
            if isinstance(ceiling, bool) or not isinstance(ceiling, int) or ceiling < 1:
                continue
            stripped = key[len("max_") :]
            field = {"rows": "n_rows"}.get(stripped, stripped)
            value = plan.get(field) if isinstance(plan, Mapping) else None
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value <= ceiling
            ):
                continue
            raise RunnerError(
                f"plan refused before generation: {field} {value} "
                f"exceeds manifest limit {key} {ceiling} "
                "(no work started; see the durable failed run record)"
            )

    def store_plan(self, plan: dict[str, Any]) -> None:
        """Store a generation plan as the current plan."""
        self._enforce_plan_bounds(plan)
        target = self._spec.get("target_rows")
        if target is not None and (
            type(plan.get("n_rows")) is not int or plan.get("n_rows") != target
        ):
            raise RunnerError(
                f"plan rows {plan.get('n_rows')} differ from requested target_rows {target}"
            )
        self._plan = copy.deepcopy(dict(plan))

    def reference_stats_record(self) -> dict[str, Any]:
        """The source-derived reference statistics (possibly empty)."""
        return self._reference_stats

    def store_reference_stats(self, reference: dict[str, Any]) -> None:
        """Store source-derived reference statistics for the checks."""
        self._reference_stats = copy.deepcopy(dict(reference))

    def companion_record(self) -> dict[str, Any]:
        """The companion bundle (possibly empty when unused)."""
        return self._companion

    def store_companion(self, bundle: dict[str, Any]) -> None:
        """Store the companion bundle for downstream steps."""
        self._companion = copy.deepcopy(dict(bundle))

    def read_latest_report(self) -> Any:
        """The most recently recorded validation report."""
        if self._latest_report_sha is None:
            raise RunnerError("no validation report recorded yet")
        return copy.deepcopy(self._reports[self._latest_report_sha])

    def read_report_for(self, sha256: str) -> Any:
        """The validation report recorded for one artifact hash."""
        try:
            return copy.deepcopy(self._reports[sha256])
        except KeyError as exc:
            raise RunnerError(
                f"no validation report recorded for artifact {sha256}"
            ) from exc

    def store_report(self, report: Any, capability_id: str) -> None:
        """Store a validation report bound to the current artifact."""
        current = self.current_artifact_sha
        if current is None:
            raise RunnerError(
                f"capability {capability_id!r} recorded a report with no "
                "artifact bound on this attempt"
            )
        self._reports[current] = copy.deepcopy(report)
        self._latest_report_sha = current

    def prior_attempt_records(self) -> tuple[dict[str, Any], ...]:
        """Immutable records of completed attempts, oldest first."""
        return tuple(attempt.to_dict() for attempt in self.attempts)

    def selected_artifact_record(self) -> str | None:
        """The engine-selected artifact hash, once a loop has decided."""
        return self._selected_artifact_sha

    def selected_plan_record(self) -> dict[str, Any]:
        """The engine-selected attempt's own plan (loud when undecided).

        The current plan may belong to a later, worse attempt; only the
        selected record's plan replays the selected artifact.
        """
        if self._best is None:
            raise RunnerError(
                "no attempt selected yet: the engine selects an attempt "
                "only after a loop decides"
            )
        return copy.deepcopy(dict(self._best.plan_ref))

    def artifact_dir_record(self) -> str | None:
        """The engine-registered artifact directory, once prepared."""
        return self._artifact_dir

    def store_advisory(self, capability_id: str, name: str, payload: Any) -> None:
        """Append one advisory output to the separate advisory store."""
        self.advisories.append(
            {"capability": capability_id, "name": name, "payload": payload}
        )

    def read_advisory(self, capability_id: str, name: str) -> Any:
        """The latest advisory payload recorded under ``name``, if any."""
        for entry in reversed(self.advisories):
            if entry["name"] == name:
                return copy.deepcopy(entry["payload"])
        return None

    def bind_check_artifact(self, explicit: Any) -> str:
        """Resolve the artifact hash one check result measures.

        An explicitly named hash must be a known stored artifact in the
        current phase; otherwise the latest artifact of the current phase
        applies. Anything else is rejected as invalid evidence.
        """
        if explicit is not None and not isinstance(explicit, str):
            raise RunnerError(
                f"check artifact_sha256 must be a string, got {explicit!r}"
            )
        if self._in_loop:
            phase = list(self.attempt_artifacts)
        elif self._best is not None:
            phase = list(self._best.artifact_refs) + list(self._post_artifacts)
        else:
            phase = list(self.attempt_artifacts)
        if explicit is not None:
            if explicit not in self.artifact_payloads:
                raise RunnerError(
                    f"check names unknown artifact {explicit!r}: refusing (no release)"
                )
            if explicit not in phase:
                raise RunnerError(
                    f"check measures artifact {explicit!r} outside the "
                    "current attempt: refusing (no release)"
                )
            return explicit
        if self.current_artifact_sha is None:
            raise RunnerError("recorded checks with no artifact bound on this attempt")
        if self.current_artifact_sha not in phase:
            raise RunnerError(
                f"check measures artifact {self.current_artifact_sha!r} "
                "outside the current attempt: refusing (no release)"
            )
        return self.current_artifact_sha

    def store_checks(
        self, capability_id: str, items: list[Mapping[str, Any]], attempt: int
    ) -> None:
        """Validate and store one capability's check results."""
        from .verdict import validate_results as _validate

        scoped = RunContext(
            capability_id=capability_id,
            permissions=_Permissions(writes_verdict=True),
            engine=self,
        )
        bound: list[EngineCheckResult] = []
        for item in items:
            sha = self.bind_check_artifact(
                item.get("artifact_sha256") if isinstance(item, Mapping) else None
            )
            bound.append(scoped._check_item(item, artifact_sha256=sha, attempt=attempt))
        _validate(
            bound,
            self.manifest.checks,
            known_artifacts=list(self.artifact_payloads),
            where=f"capability {capability_id!r}",
        )
        self.pending_checks.extend(bound)

    def prepare_artifact_dir(self, capability_id: str) -> Path:
        """Return the run's own directory and sync the payload cache.

        The directory is ``runs/<run-id>/``: every execution owns its
        history, so identical output bytes never overwrite a previous
        run's plan, log, decision, or release reference. Payload bytes
        are additionally mirrored into the content-addressed
        ``runs/<payload-sha>/`` cache for readers resolving bytes by
        hash; run-scoped history never lives there.
        """
        sha = self._selected_artifact_sha
        if sha is None:
            raise RunnerError(
                f"capability {capability_id!r} cannot prepare an artifact "
                "directory before the engine selects an artifact"
            )
        artifact_dir = self.store.run_dir
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self._artifact_dir = str(artifact_dir)
        try:
            payload = self.artifact_payloads[sha]
        except KeyError as exc:
            raise RunnerError(f"no stored payload for selected artifact {sha}") from exc
        cache_files: dict[str, bytes] = {"data.csv": bytes(payload)}
        image_sha = self._companion.get("image_artifact_sha")
        if isinstance(image_sha, str) and image_sha in self.artifact_payloads:
            cache_files["synthetic.png"] = bytes(self.artifact_payloads[image_sha])
        self.store.sync_payload_cache(sha, cache_files)
        return artifact_dir

    def provenance_record(self) -> str:
        """Render the release provenance statement from engine state.

        Output terms resolve from the recorded rights evidence only: a
        public-dedication sentence appears exactly when the source hash
        verified against the sibling manifest at intake and that verified
        record carries an explicit output-terms grant; otherwise the
        statement carries an explicit no-clearance notice and no
        dedication is implied.
        """
        from ..artifacts.package import (
            build_provenance as _build,
        )
        from ..artifacts.package import (
            resolve_output_terms as _resolve,
        )

        meta = self._source_meta
        verdict, _ = self.final_decision()
        self._final_verdict = verdict
        if self._best is not None and isinstance(self._best.plan_ref, Mapping):
            plan = dict(self._best.plan_ref)
        else:
            plan = dict(self._plan) if isinstance(self._plan, Mapping) else {}
        verified = meta.get("hash_verified") is True
        manifest_name = meta.get("manifest_name")
        return _build(
            source_name=str(meta.get("source_name", "")),
            source_sha256=str(meta.get("source_sha256", "")),
            licence=meta.get("licence"),
            attribution=meta.get("attribution"),
            artifact_sha256=str(self._selected_artifact_sha or ""),
            plan=plan,
            verdict=verdict,
            run_id=self.log.run_id,
            output_terms=_resolve(
                hash_verified=verified,
                output_terms_grant=meta.get("output_terms_grant"),
            ),
            hash_verified=verified,
            manifest_name=(
                str(manifest_name) if isinstance(manifest_name, str) else None
            ),
        )

    def freeze_release_candidates(self) -> dict[str, Any]:
        """Snapshot every release candidate's bytes before the prompt.

        Runs when the run reaches the human gate, after the artifact
        step and before the review prompt is shown. Each frozen member
        declared by the profile is read once from the run directory and
        kept in engine memory plus a ``frozen/`` copy; the candidate
        manifest (path, size, sha256, produced-by attempt) is recorded
        to ``frozen-manifest.json`` and its digest becomes the evidence
        digest the human decision binds. A working file mutated or
        deleted during the review prompt cannot change the snapshot.
        Missing required members refuse loudly instead of shipping
        short. Runs with no artifact directory (no package step ran
        before the gate) have no candidates: the freeze is a recorded
        no-op and the decision binds nothing.
        """
        from ..artifacts import package as _package

        if self._artifact_dir is None:
            self.log.append(
                "freeze",
                "skipped",
                {"reason": "no artifact directory prepared"},
            )
            return {
                "run_id": self.run.id,
                "artifact_sha256": self._selected_artifact_sha,
                "evidence_digest": None,
                "members": [],
            }
        if self._best is None:
            raise RunnerError(
                "cannot freeze release candidates before an attempt "
                "is selected: refusing (no release)"
            )
        run_dir = Path(self._artifact_dir)
        images_present = self.run_conditions()["images_present"]
        specs = _package.selected_release_members(
            self.manifest, images_present=images_present
        )
        # Strict only under a declared release contract: manifests that
        # declare no release block (test-only shapes) snapshot whatever
        # candidates exist, and packaging -- if the flow packs at all --
        # still refuses any gap.
        strict = bool(getattr(self.manifest, "release", None))
        frozen: dict[str, bytes] = {}
        entries: list[dict[str, Any]] = []
        attempt_no = self._best.n
        for spec in specs:
            name = spec["name"]
            if spec.get("source", "frozen") != "frozen":
                continue
            path = run_dir / name
            if not path.is_file():
                if strict and spec.get("required", True):
                    raise RunnerError(
                        f"frozen release candidate {name!r} is missing: "
                        "refusing (no release)"
                    )
                continue
            payload = path.read_bytes()
            frozen[name] = payload
            entries.append(
                {
                    "path": name,
                    "size": len(payload),
                    "sha256": _package.sha256_bytes(bytes(payload)),
                    "attempt": attempt_no,
                }
            )
        digest = _package.candidate_digest(entries)
        self._frozen_files = frozen
        self._frozen_entries = entries
        self._frozen_attempt = attempt_no
        self._evidence_digest = digest
        _package.write_frozen_snapshot(run_dir, frozen)
        record = {
            "run_id": self.run.id,
            "artifact_sha256": self._selected_artifact_sha,
            "evidence_digest": digest,
            "members": entries,
        }
        _package.write_frozen_manifest(run_dir, record)
        self.log.append(
            "freeze",
            "ok",
            {
                "members": [entry["path"] for entry in entries],
                "evidence_digest": digest,
            },
            artifact_sha256=self._selected_artifact_sha,
        )
        return record

    def _rights_record(self) -> dict[str, Any]:
        """The release-manifest rights evidence for the recorded source."""
        from ..artifacts.package import (
            resolve_output_terms as _resolve,
        )
        from ..artifacts.package import (
            rights_block as _rights,
        )

        meta = self._source_meta
        verified = meta.get("hash_verified") is True
        return _rights(
            source_sha256=str(meta.get("source_sha256", "")),
            licence=meta.get("licence"),
            attribution=meta.get("attribution"),
            manifest_name=meta.get("manifest_name"),
            manifest_sha256=meta.get("manifest_sha256"),
            hash_verified=verified,
            output_terms=_resolve(
                hash_verified=verified,
                output_terms_grant=meta.get("output_terms_grant"),
            ),
            output_terms_grant=meta.get("output_terms_grant"),
        )

    def evidence_inputs(self) -> dict[str, Any]:
        """Assemble the stored-record bundle the evidence report renders.

        Every value comes from a record the run already persisted (the
        run record, the selected attempt receipt, the frozen candidate
        manifest, the human decision, the rights evidence): the report
        renderer reads this bundle and recomputes nothing.
        """
        best = self._best
        conditions = self.run_conditions()
        refs: dict[str, str] = {
            "run": "run.json",
            "spec": "run.jsonl",
            "source": "run.jsonl",
            "release": "manifest.json",
        }
        if best is not None:
            attempt_path = f"attempts/attempt-{best.n}/receipt.json"
            refs["attempt"] = attempt_path
            refs["checks"] = attempt_path
            attempt_record: dict[str, Any] = best.to_dict()
            plan = dict(attempt_record.get("plan_ref") or {})
            check_results = list(attempt_record.get("check_results") or [])
            attempt_no: int | None = best.n
        else:
            plan = dict(self._plan) if isinstance(self._plan, Mapping) else {}
            check_results = []
            attempt_no = None
        if self._evidence_digest is not None:
            refs["frozen"] = "frozen-manifest.json"
        if self._decision is not None:
            refs["decision"] = "decision.json"
        return {
            "run_id": self.log.run_id,
            "profile_id": self.manifest.id,
            "profile_version": self.manifest.version,
            "spec": self.spec_record(),
            "spec_digest": str(self._spec_digest),
            "source": copy.deepcopy(dict(self._source_meta)),
            "attempt_no": attempt_no,
            "plan": plan,
            "model_policy": self.model_policy,
            "plan_origin": "saved-plan replay"
            if self._fixed_plan is not None
            else "model proposal",
            "model_invocations": [
                copy.deepcopy(event["details"])
                for event in self.log.events
                if event["stage"] == "model_invocation"
            ],
            "model_denials": [
                copy.deepcopy(event["details"])
                for event in self.log.events
                if event["stage"] == "model_policy_denied"
            ],
            "model_failures": [
                copy.deepcopy(event["details"])
                for event in self.log.events
                if event["stage"] == "model_call_failed"
            ],
            "advisories": copy.deepcopy(self.advisories),
            "check_results": check_results,
            "manifest_checks": [dict(entry) for entry in self.manifest.checks],
            "conditions": dict(conditions),
            "decision": (
                copy.deepcopy(dict(self._decision))
                if isinstance(self._decision, Mapping)
                else None
            ),
            "frozen_digest": self._evidence_digest,
            "frozen_members": (
                copy.deepcopy(list(self._frozen_entries))
                if isinstance(self._frozen_entries, list)
                else []
            ),
            "rights": self._rights_record(),
            "images_present": bool(conditions.get("images_present", False)),
            "refs": refs,
        }

    def pack_recorded_release(self, capability_id: str) -> tuple[Path, str]:
        """Write the reproducible release ZIP from the frozen snapshot.

        Reads only the frozen snapshot plus the engine-serialised run
        log -- never mutable working files. The frozen bytes are
        re-verified against the decision-bound digest immediately
        before the build, and the build is atomic: a temporary file is
        verified and renamed into place, so any failure leaves no
        partial ``release.zip``.
        """
        from ..artifacts import package as _package

        if self._artifact_dir is None:
            raise RunnerError(
                f"capability {capability_id!r} cannot pack a release "
                "before the artifact directory is prepared"
            )
        if (
            self._frozen_files is None
            or self._frozen_entries is None
            or self._evidence_digest is None
        ):
            raise RunnerError(
                f"capability {capability_id!r} cannot pack a release "
                "with no frozen snapshot: refusing to read mutable "
                "working files (no release)"
            )
        if (
            self._decision is None
            or self._decision.get("frozen_manifest_sha256") != self._evidence_digest
        ):
            raise RunnerError(
                "the human decision does not bind the frozen manifest: "
                "refusing (no release)"
            )
        if _package.candidate_digest(self._frozen_entries) != self._evidence_digest:
            raise RunnerError(
                "the frozen snapshot changed after the human decision: "
                "refusing (no release)"
            )
        for entry in self._frozen_entries:
            payload = self._frozen_files.get(entry["path"])
            if (
                payload is None
                or _package.sha256_bytes(bytes(payload)) != entry["sha256"]
            ):
                raise RunnerError(
                    f"frozen member {entry['path']!r} failed "
                    "reverification: refusing (no release)"
                )
        from .report import render_evidence_report as _render

        run_dir = Path(self._artifact_dir)
        images_present = self.run_conditions()["images_present"]
        specs = _package.selected_release_members(
            self.manifest, images_present=images_present
        )
        report_bytes = _render(self.evidence_inputs()).encode("utf-8")
        (run_dir / ENGINE_REPORT_MEMBER).write_text(
            report_bytes.decode("utf-8"), encoding="utf-8"
        )
        report_declared = any(
            isinstance(entry, Mapping)
            and entry.get("name") == ENGINE_REPORT_MEMBER
            and entry.get("source", "frozen") == ENGINE_REPORT_SOURCE
            for entry in specs
        )
        files: dict[str, bytes] = {}
        for spec in specs:
            name = spec["name"]
            if (
                spec.get("source", "frozen") == ENGINE_REPORT_SOURCE
                and name == ENGINE_REPORT_MEMBER
            ):
                # The evidence report is rendered by the engine at pack
                # time from stored records (it cites the human decision,
                # which postdates the frozen snapshot). It is a view over
                # the frozen evidence plus the decision, never new
                # evidence itself.
                if report_declared:
                    files[name] = bytes(report_bytes)
                continue
            if spec.get("source", "frozen") != "frozen":
                # The run log is serialised by the engine from its own
                # in-memory events at pack time (last event: the human
                # review). It is never read from working files.
                files[name] = self.log.to_jsonl().encode("utf-8")
                continue
            try:
                files[name] = bytes(self._frozen_files[name])
            except KeyError as exc:
                if spec.get("required", True):
                    raise RunnerError(
                        f"frozen member {name!r} is missing: refusing (no release)"
                    ) from exc
        zip_path, zip_digest = _package.write_release_zip(run_dir, files)
        try:
            entries = _package.candidate_entries(
                files, attempt=self._frozen_attempt or 0
            )
            verdict, failed = self.final_decision()
            required_checks = applicable_required_ids(
                self.manifest.checks, conditions=self.run_conditions()
            )
            _package.write_release_manifest(
                run_dir,
                {
                    "run_id": self.run.id,
                    "artifact_sha256": self._selected_artifact_sha,
                    "evidence_digest": self._evidence_digest,
                    "manifest_digest": _package.candidate_digest(entries),
                    "members": entries,
                    "rights": self._rights_record(),
                    "acceptance": {
                        "verdict": verdict,
                        "failed": list(failed),
                        "required_checks": sorted(required_checks),
                        "request_identity": {
                            "profile": self.run.profile_ref,
                            "source_sha256": str(
                                self._source_meta.get("source_sha256", "")
                            ),
                            "spec_digest": str(self._spec_digest),
                        },
                    },
                },
            )
            _package.write_detached_hash(run_dir, zip_digest)
        except BaseException:
            for leftover in (
                run_dir / _package.RELEASE_ZIP_NAME,
                run_dir / _package.RELEASE_HASH_NAME,
                run_dir / _package.RELEASE_MANIFEST_NAME,
            ):
                try:
                    leftover.unlink()
                except OSError:
                    pass
            raise
        return zip_path, zip_digest

    def run_conditions(self) -> dict[str, bool]:
        """Resolve the applicability conditions for the final verdict."""
        return {
            CONDITION_IMAGES_PRESENT: (self._companion.get("reference") is not None),
        }

    def final_decision(self) -> tuple[str, list[str]]:
        """Compute the engine-owned final verdict from validated evidence."""
        best_results = list(self._best.check_results) if self._best is not None else []
        evidence = best_results + list(self.pending_checks)
        required = applicable_required_ids(
            self.manifest.checks, conditions=self.run_conditions()
        )
        return decide(
            evidence,
            self.manifest.checks,
            required_ids=required,
            known_artifacts=list(self.artifact_payloads),
            where="final acceptance",
        )

    def final_verdict_record(self, verdict: str, failed: list[str]) -> None:
        """Record the engine-owned final verdict decided at the gate."""
        self._final_verdict = verdict
        self._final_failed = list(failed)

    def final_verdict_recorded(self) -> bool:
        """Whether the gate has recorded the engine-owned final verdict."""
        return self._final_verdict is not None

    def final_verdict_value(self) -> str | None:
        """The engine-owned final verdict, once recorded at the gate."""
        return self._final_verdict

    def final_failed_value(self) -> list[str]:
        """The unsatisfied required checks from the final verdict."""
        return list(self._final_failed)

    def persist_terminal_record(self, status: str, error: str | None = None) -> None:
        """Mark the durable run record terminal (never overwrites history).

        The per-run directory already holds every event write-through;
        this only stamps the terminal status, the error (if any), the
        human decision, and the selected artifact onto ``run.json``.
        """
        source_sha = self._source_meta.get("source_sha256", "")
        self.store.update_run(
            status,
            error=error,
            attempts=len(self.attempts),
            artifact_sha256=self.selected_artifact_record(),
            source_sha256=(source_sha if isinstance(source_sha, str) else ""),
            decision=self._decision,
        )

    def loop_verdict_value(self) -> str | None:
        """The engine-owned loop verdict, once a loop has decided."""
        return self._loop_verdict

    def loop_failed_value(self) -> list[str]:
        """The unsatisfied checks from the engine-owned loop verdict."""
        return list(self._loop_failed)

    def best_record(self) -> Attempt | None:
        """The engine-selected best attempt, once a loop has decided."""
        return self._best

    def check_model_policy(self, role: str) -> None:
        from .model_policy import ModelPolicyError, enforce_model_policy

        config = self.role_bindings.get(role, {})
        try:
            enforce_model_policy(self.model_policy, config)
        except ModelPolicyError as exc:
            self.log.append(
                "model_policy_denied",
                "refused",
                {
                    "role": role,
                    "policy": self.model_policy,
                    "error": str(exc),
                },
            )
            raise

    def model_for(self, role: str) -> Any:
        """Resolve a model role to a client, enforcing availability."""
        self.check_model_policy(role)
        if role in self._models:
            return self._models[role]
        if role not in self.role_bindings:
            raise PermissionDenied(f"model role {role!r} is not bound by the manifest")
        config = self.role_bindings[role]
        if config.get("enabled", True) is False:
            raise PermissionDenied(
                f"model role {role!r} is disabled: treated as missing"
            )
        client = self.model_factory(
            role,
            {
                **config,
                "local_only": self.model_policy == "local-only",
            },
        )
        guarded = _BudgetedClient(client, self, role)
        self._models[role] = guarded
        return guarded

    def store_artifact(
        self, name: str, media_type: str, payload: bytes, produced_by: str
    ) -> Artifact:
        from ..artifacts.hashing import sha256_bytes

        if not isinstance(payload, (bytes, bytearray)):
            raise RunnerError(
                f"capability {produced_by!r} stored an artifact payload "
                f"that is not bytes (got {type(payload).__name__})"
            )
        if not isinstance(name, str) or not name:
            raise RunnerError("artifact name must be a non-empty string")
        digest = sha256_bytes(bytes(payload))
        self.artifact_payloads[digest] = bytes(payload)
        self.current_artifact_sha = digest
        self.attempt_artifacts.append(digest)
        if not self._in_loop:
            self._post_artifacts.append(digest)
        record = Artifact(
            sha256=digest,
            media_type=media_type,
            produced_by=produced_by,
            attempt=len(self.attempts) + 1,
            name=name,
        )
        self.artifact_records.append(record)
        return record

    def latest_artifact_record(self) -> Artifact:
        """The record for the most recently stored artifact."""
        if not self.artifact_records:
            raise RunnerError("no artifact recorded yet")
        return self.artifact_records[-1]

    def scoped(self, capability_id: str) -> RunContext:
        spec = permission_of(self.manifest, capability_id)
        permissions = _Permissions(
            reads_raw_source=spec["reads_raw_source"],
            calls_model=spec["calls_model"],
            advisory=spec["advisory"],
            writes_verdict=spec["writes_verdict"],
        )
        return RunContext(
            capability_id=capability_id,
            permissions=permissions,
            engine=self,
        )

    def run_capability(self, capability_id: str) -> None:
        """Run one capability, enforcing the critic failure policy.

        A capability declaring ``critic_policy: optional`` never fails
        the run: any failure (including budget exhaustion) is recorded
        visibly as an advisory failure and the run continues on its
        deterministic evidence, which alone decides the verdict. A
        ``required`` critic -- or no declared policy -- fails loudly.
        Permission denials always propagate: they are authority errors,
        never advisory output.
        """
        policy = self.manifest.capabilities.get(capability_id, {}).get("critic_policy")
        try:
            self.registry[capability_id](self.scoped(capability_id))
        except PermissionDenied:
            raise
        except BudgetExceeded as exc:
            if policy != "optional":
                raise
            self._record_advisory_failure(capability_id, exc, budget_exhausted=True)
        except Exception as exc:  # noqa: BLE001 -- policy-recorded
            if policy != "optional":
                raise
            self._record_advisory_failure(capability_id, exc, budget_exhausted=False)

    def _record_advisory_failure(
        self,
        capability_id: str,
        exc: Exception,
        *,
        budget_exhausted: bool,
    ) -> None:
        """Record an optional critic's failure without touching the verdict."""
        reason = f"{type(exc).__name__}: {exc}"
        payload = {
            "status": "advisory-failed",
            "error": reason,
            "budget_exhausted": budget_exhausted,
        }
        self.store_advisory(capability_id, "advisory-failed", payload)
        self.log.append(
            f"{capability_id}:advisory",
            "advisory-failed",
            {"name": "advisory-failed", **payload},
        )
        cause = "budget exhausted; " if budget_exhausted else ""
        self.say(
            f"[{capability_id}] advisory failed ({cause}recorded, "
            f"run continues on deterministic evidence): {exc}"
        )

    def enforce_limits(self) -> None:
        """Enforce manifest limits against the current plan, when present."""
        max_rows = self.manifest.limits.get("max_rows")
        plan = self._plan
        if (
            isinstance(max_rows, int)
            and isinstance(plan, Mapping)
            and isinstance(plan.get("n_rows"), int)
            and not isinstance(plan.get("n_rows"), bool)
            and plan["n_rows"] > max_rows
        ):
            raise RunnerError(
                f"plan n_rows {plan['n_rows']} exceeds limit max_rows {max_rows}"
            )

    def run_loop(self, node: Mapping[str, Any]) -> str:
        """Execute a loop node; return the engine-owned attempt verdict.

        Each attempt's verdict comes only from the required-check contract
        over that attempt's validated results: every unconditional required
        check covered exactly once with status ``"pass"``. Conditional
        checks (applicability-gated, e.g. image checks) never drive loop
        decisions; they are decided at final acceptance.
        """
        body = list(node["loop"])
        on_fail = str(node["on_fail"])
        stage = str(node.get("stage", "loop"))
        bound = node.get(MAX_ATTEMPTS_BINDING)
        if bound != MAX_ATTEMPTS_BINDING:
            raise RunnerError("loop nodes bind max_attempts to the caller supply only")
        required = unconditional_required_ids(self.manifest.checks)
        best: Attempt | None = None
        previous_failed: int | None = None
        self._in_loop = True
        self._post_artifacts = []
        try:
            for attempt_number in range(1, self.max_attempts + 1):
                self.pending_checks = []
                self.attempt_artifacts = []
                # Bounds are validated before work: an oversize plan is
                # refused here -- before the generator runs -- as well
                # as at plan-store time.
                self.enforce_limits()
                tokens_before = self.model_tokens
                for capability_id in body:
                    self.run_capability(capability_id)
                self.enforce_limits()
                verdict, failed = decide(
                    list(self.pending_checks),
                    self.manifest.checks,
                    required_ids=required,
                    known_artifacts=list(self.artifact_payloads),
                    phase_artifacts=list(self.attempt_artifacts),
                    where=f"{stage} attempt {attempt_number}",
                )
                plan_snapshot = (
                    copy.deepcopy(self._plan) if isinstance(self._plan, Mapping) else {}
                )
                source_sha = self._source_meta.get("source_sha256", "")
                attempt = Attempt(
                    n=attempt_number,
                    plan_ref=plan_snapshot,
                    artifact_refs=list(self.attempt_artifacts),
                    check_results=list(self.pending_checks),
                    status=verdict,
                    manifest_digest=self.manifest.digest,
                    source_sha256=(source_sha if isinstance(source_sha, str) else ""),
                    seed=plan_snapshot.get("seed"),
                    tokens=self.model_tokens - tokens_before,
                    spec_digest=str(self._spec_digest),
                )
                self.attempts.append(attempt)
                self.store.write_attempt(
                    attempt,
                    [
                        {
                            "sha256": record.sha256,
                            "media_type": record.media_type,
                            "name": record.name,
                            "produced_by": record.produced_by,
                            "attempt": record.attempt,
                        }
                        for record in self.artifact_records
                        if record.sha256 in set(self.attempt_artifacts)
                    ],
                )
                self.log.append(
                    stage,
                    "ok" if verdict == ATTEMPT_PASS else "fail",
                    {
                        "attempt": attempt_number,
                        "verdict": verdict,
                        "failed": failed,
                        "plan": plan_snapshot,
                    },
                    usage=self._last_usage,
                    artifact_sha256=(
                        self.attempt_artifacts[-1] if self.attempt_artifacts else None
                    ),
                )
                if best is None or len(failed) < len(best.failed):
                    best = attempt
                self.say(
                    f"[{stage}] attempt {attempt_number}: verdict={verdict} "
                    f"failed={failed}"
                )
                if verdict == ATTEMPT_PASS:
                    break
                if attempt_number == self.max_attempts:
                    break
                if previous_failed is not None and len(failed) >= previous_failed:
                    self.say(f"[{stage}] no improvement across two attempts: stopping")
                    break
                previous_failed = len(failed)
                repair_role = self.manifest.capabilities[on_fail].get("calls_model")
                if repair_role:
                    from .model_policy import ModelPolicyError

                    try:
                        self.check_model_policy(str(repair_role))
                    except ModelPolicyError as exc:
                        self.say(
                            f"[{stage}] repair unavailable: {exc}; failing checks retained"
                        )
                        break
                self.run_capability(on_fail)
        finally:
            self._in_loop = False
        assert best is not None
        # The engine alone selects the attempt, its artifact, and the loop
        # verdict. Capabilities cannot reach any of these.
        self._best = best
        self._post_artifacts = []
        self.pending_checks = []
        self._selected_artifact_sha = (
            best.artifact_refs[-1] if best.artifact_refs else None
        )
        self._loop_verdict = best.status
        self._loop_failed = list(best.failed)
        return best.status

    def run_gate(self, gate_id: str) -> Decision:
        """Prompt the human and record the decision verbatim.

        The decision binds the frozen evidence digest snapshotted when
        the run reached the gate, so the released bytes are exactly the
        bytes the human reviewed. Runs with no frozen snapshot (no
        package step ran) bind nothing; their later packaging -- if
        any -- refuses without it.
        """
        if self._artifact_dir is not None and self._evidence_digest is None:
            raise RunnerError(
                f"no frozen snapshot bound at gate {gate_id!r}: refusing (no release)"
            )
        self.say("Human review required: the model cannot accept its own output.")
        try:
            decision = self._ask("Decision [accept/modify/reject]: ").strip().lower()
            note = self._ask("Note (recorded verbatim): ").rstrip("\r")
        except EOFError as exc:
            self.say(f"[{gate_id}] error: {exc}")
            raise RunnerError(f"no human decision for gate {gate_id!r}") from exc
        record = Decision(verdict=decision, note=note, decided_by="human").with_time()
        self._decision = {
            "verdict": record.verdict,
            "note": record.note,
            "decided_by": record.decided_by,
            "at": record.at,
        }
        if self._evidence_digest is not None:
            self._decision["frozen_manifest_sha256"] = self._evidence_digest
        self.store.write_decision(self._decision)
        self.log.append(
            gate_id,
            decision if decision in ("accept", "modify", "reject") else "invalid",
            {"decision": decision, "note": note},
            artifact_sha256=self._selected_artifact_sha,
        )
        return record


def execute(
    manifest: Manifest,
    registry: Mapping[str, CapabilityFn],
    *,
    source: Path,
    max_attempts: int,
    model_factory: ModelFactory,
    out: Path,
    say: Callable[[str], None] | None = None,
    ask: Callable[[str], str] | None = None,
    images_dir: Path | None = None,
    local_critic: bool = False,
    role_bindings: Mapping[str, Mapping[str, Any]] | None = None,
    role_binding_sources: Mapping[str, Mapping[str, str]] | None = None,
    spec: Mapping[str, Any] | None = None,
    fixed_plan: Mapping[str, Any] | None = None,
    model_policy: str = "hosted",
) -> tuple[int, dict[str, Any]]:
    """Execute a manifest graph; return (exit code, outcome summary).

    ``max_attempts`` is required and has no default: the caller supplies
    the bound and every loop node binds to it. ``local_critic`` enables
    the optional advisory text-critique step (default off); advisory
    output is logged and never affects the verdict. ``role_bindings``
    carries the composition root's effective per-role configs (after
    CLI/env/profile/default resolution); when omitted the manifest's own
    bindings apply. ``spec`` is an optional GenerationSpec overlay: it
    is normalised against the manifest and the caller flags before
    anything else runs, and an unsupported request is refused with a
    durable terminal record before any model call and before any
    generation. ``fixed_plan`` is an optional saved plan to replay: the
    planning step validates and stores it instead of requesting a new
    plan, so no planner call is made.
    """
    import sys as _sys

    out_stream = _sys.stdout
    in_stream = _sys.stdin

    def _say(message: str) -> None:
        if say is not None:
            say(message)
        else:
            print(message, file=out_stream)

    def _ask(prompt: str) -> str:
        if ask is not None:
            return ask(prompt)
        _say(prompt)
        line = in_stream.readline()
        if line == "":
            raise EOFError(f"no input for prompt: {prompt!r}")
        return line.rstrip("\n")

    from .model_policy import ModelPolicyError, validate_policy
    from .spec import SpecError
    from .spec import normalize_spec as _normalize

    try:
        validate_policy(model_policy)
        normalized_spec, spec_digest = _normalize(
            spec,
            manifest,
            source=Path(source),
            max_attempts=max_attempts,
            images_dir=images_dir,
        )
    except (SpecError, ModelPolicyError) as exc:
        refused_dir = record_refusal(
            Path(out),
            profile_ref=manifest.id,
            request={
                "source": str(source),
                "max_attempts": max_attempts,
                "out": str(out),
                **({"images_dir": str(images_dir)} if images_dir is not None else {}),
                "spec": dict(spec) if isinstance(spec, Mapping) else spec,
                "model_policy": model_policy,
            },
            error=str(exc),
        )
        _say(f"[engine] spec refused: {exc}")
        return EXIT_FAILED, {
            "status": "refused",
            "run_id": refused_dir.name,
            "profile": manifest.id,
            "verdict": None,
            "failed": [],
            "artifact_sha256": None,
            "artifact_dir": None,
            "attempts": 0,
            "advisories": [],
            "error": str(exc),
        }
    try:
        runner = _Runner(
            manifest=manifest,
            registry=registry,
            source=Path(source),
            max_attempts=max_attempts,
            model_factory=model_factory,
            out=Path(out),
            say=_say,
            ask=_ask,
            images_dir=images_dir,
            local_critic=local_critic,
            role_bindings=role_bindings,
            role_binding_sources=role_binding_sources,
            spec=normalized_spec,
            spec_digest=spec_digest,
            fixed_plan=fixed_plan,
            model_policy=model_policy,
        )
    except (RunnerError, PermissionDenied) as exc:
        _say(f"[engine] composition refused: {exc}")
        return EXIT_FAILED, {
            "status": "error",
            "run_id": None,
            "profile": manifest.id,
            "verdict": None,
            "failed": [],
            "artifact_sha256": None,
            "artifact_dir": None,
            "attempts": 0,
            "advisories": [],
            "error": str(exc),
        }
    runner.log.append("run_start", "ok", {"run": runner.run.to_dict()})
    runner.log.append(
        "spec",
        "ok",
        {"spec": normalized_spec, "spec_digest": spec_digest},
    )
    runner.log.append(
        "model_bindings",
        "ok",
        {
            "roles": {
                role: {
                    "adapter": config.get("adapter", "meta_cli"),
                    "model": config.get("model"),
                    "reasoning_effort": config.get("reasoning_effort"),
                    **(
                        {"base_url": config.get("base_url")}
                        if config.get("adapter") == "glimmer_local"
                        else {}
                    ),
                    "sources": dict(runner.role_binding_sources.get(role, {})),
                }
                for role, config in runner.role_bindings.items()
            },
            **(
                {"budget_max_tokens_per_run": runner.model_budget}
                if runner.model_budget is not None
                else {}
            ),
        },
    )
    gate_id = ""
    try:
        for node in manifest.graph:
            if "step" in node:
                runner.run_capability(str(node["step"]))
            elif "loop" in node:
                runner.run_loop(node)
            elif "gate" in node:
                gate_id = str(node["gate"])
                # The engine alone decides acceptance, from validated check
                # evidence only. Advisory output is never consulted here.
                verdict, failed = runner.final_decision()
                runner.final_verdict_record(verdict, failed)
                if verdict != ATTEMPT_PASS:
                    return _refuse_gate(runner, gate_id, failed)
                # Freeze every release candidate before the prompt: the
                # human reviews frozen bytes, and packaging later reads
                # only the snapshot.
                runner.freeze_release_candidates()
                decision = runner.run_gate(gate_id)
                if runner.artifact_dir_record() is not None:
                    runner.log.write_jsonl(
                        Path(str(runner.artifact_dir_record())) / "run.jsonl"
                    )
                if decision.verdict == "accept":
                    continue
                if decision.verdict in ("modify", "reject"):
                    runner.say(f"[{gate_id}] {decision.verdict}: no release written")
                    runner.persist_terminal_record("declined")
                    return EXIT_DECLINED, _outcome(runner, "declined")
                runner.say(
                    f"[{gate_id}] invalid decision {decision.verdict!r}: "
                    "no release written"
                )
                runner.persist_terminal_record("invalid")
                return EXIT_DECLINED, _outcome(runner, "invalid")
    except PermissionDenied as exc:
        runner.say(f"[engine] permission denied: {exc}")
        runner.log.append("engine", "error", {"error": str(exc)})
        runner.persist_terminal_record("error", str(exc))
        return EXIT_FAILED, _outcome(runner, "error", str(exc))
    except RunnerError as exc:
        runner.say(f"[engine] error: {exc}")
        runner.log.append("engine", "error", {"error": str(exc)})
        runner.persist_terminal_record("error", str(exc))
        return EXIT_FAILED, _outcome(runner, "error", str(exc))
    except Exception as exc:  # noqa: BLE001 -- capability fault, logged
        runner.say(f"[engine] capability failed: {exc}")
        runner.log.append("engine", "error", {"error": str(exc)})
        runner.persist_terminal_record("error", str(exc))
        return EXIT_FAILED, _outcome(runner, "error", str(exc))
    runner.persist_terminal_record("released")
    return EXIT_OK, _outcome(runner, "released")


def _outcome(runner: _Runner, status: str, error: str | None = None) -> dict[str, Any]:
    if runner.final_verdict_recorded():
        verdict: str | None = runner.final_verdict_value()
        failed = list(runner.final_failed_value())
    else:
        verdict = runner.loop_verdict_value()
        failed = list(runner.loop_failed_value())
    outcome: dict[str, Any] = {
        "status": status,
        "run_id": runner.run.id,
        "profile": runner.manifest.id,
        "verdict": verdict,
        "failed": failed,
        "artifact_sha256": runner.selected_artifact_record(),
        "artifact_dir": runner.artifact_dir_record(),
        "attempts": len(runner.attempts),
        "advisories": list(runner.advisories),
    }
    if error is not None:
        outcome["error"] = error
    return outcome


def _evidence_details(runner: _Runner) -> dict[str, str]:
    """Map failed check ids to their detail strings for gate reporting."""
    details: dict[str, str] = {}
    if runner.best_record() is not None:
        for result in runner.best_record().check_results:
            if result.status != "pass":
                details[result.check_id] = result.detail
    for result in runner.pending_checks:
        if result.status != "pass":
            details[result.check_id] = result.detail
    return details


def _refuse_gate(
    runner: _Runner, gate_id: str, failed: list[str]
) -> tuple[int, dict[str, Any]]:
    """Decline the gate on a failing verdict: nothing to accept.

    The artifact steps before the gate still run, so the failing evidence
    is stored; the human is never asked to accept a failing run.
    """
    details = _evidence_details(runner)
    artifact_dir = runner.artifact_dir_record()
    runner.say(f"[{gate_id}] skipped: validation failed, nothing to accept")
    for check_id in failed:
        runner.say(f"[fail] {check_id}")
    for check_id in failed:
        detail = details.get(check_id, "")
        if detail:
            runner.say(f"[fail] {check_id}: {detail}")
    if artifact_dir is not None:
        from .report import render_evidence_report as _render

        (Path(str(artifact_dir)) / ENGINE_REPORT_MEMBER).write_text(
            _render(runner.evidence_inputs()), encoding="utf-8"
        )
        runner.log.append(
            gate_id,
            "skipped",
            {"reason": "failing verdict", "failed": failed},
            artifact_sha256=runner.selected_artifact_record(),
        )
        runner.log.write_jsonl(Path(str(artifact_dir)) / "run.jsonl")
    runner.persist_terminal_record("failed")
    return EXIT_FAILED, _outcome(runner, "failed")
