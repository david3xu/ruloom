"""Graph execution with mechanical permission enforcement.

The runner walks the manifest graph, which uses only three node kinds:

* ``step`` -- run one capability;
* ``loop`` -- run a body of capabilities, invoking ``on_fail`` between
  attempts, bounded by the caller-supplied ``max_attempts`` (no default);
* ``gate`` -- stop for a human decision recorded verbatim.

Enforcement is mechanical, not conventional. Raw source bytes are handed
only to capabilities declaring ``reads_raw_source``. A model client is
handed only to capabilities declaring a ``calls_model`` role, and only
when that role resolves to an enabled binding. Advisory outputs are logged
and excluded from the pass/fail computation. Only capabilities declaring
``writes_verdict`` may record check results, and every recorded check must
be declared in the manifest; the engine binds the artifact hash and the
verifier itself.

Loop policy (engine-owned): keep the best attempt by check score, stop on
the bound or on no improvement across two consecutive attempts. Exhaustion
reports the failing checks and never ships as passing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .manifest import (
    HOSTED_ADAPTER,
    LOCAL_ADAPTER,
    MAX_ATTEMPTS_BINDING,
    Manifest,
    permission_of,
)
from .records import Artifact, Attempt, Decision, EngineCheckResult, Run
from ..audit.runlog import RunLog

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
CapabilityFn = Callable[["RunContext",], None]

#: Builds a model client for a resolved role configuration.
ModelFactory = Callable[[str, Mapping[str, Any]], Any]


@dataclass
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
    call accumulates on the runner.
    """

    def __init__(self, client: Any, runner: "_Runner") -> None:
        self._client = client
        self._runner = runner

    def _check_budget(self) -> None:
        budget = self._runner.model_budget
        if budget is not None and self._runner.model_tokens >= budget:
            raise BudgetExceeded(
                f"model token budget exceeded: {self._runner.model_tokens} "
                f"tokens used of {budget} allowed; refusing the next model "
                "call before making it"
            )

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Any:
        self._check_budget()
        completion = self._client.complete(prompt, schema)
        self._runner.record_model_usage(completion.usage)
        return completion

    def complete_with_image(
        self, prompt: str, schema: Mapping[str, Any], image_path: Any
    ) -> Any:
        self._check_budget()
        completion = self._client.complete_with_image(
            prompt, schema, image_path
        )
        self._runner.record_model_usage(completion.usage)
        return completion

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_client"], name)


class RunContext:
    """The scoped view one capability execution receives.

    Profiles never touch the runner, the log, or other capabilities
    directly; every access passes through this object so the runner can
    enforce the declared permissions.
    """

    def __init__(
        self,
        *,
        capability_id: str,
        permissions: _Permissions,
        manifest: Manifest,
        state: dict[str, Any],
        runner: "_Runner",
    ) -> None:
        self.capability_id = capability_id
        self.permissions = permissions
        self.manifest = manifest
        self.state = state
        self._runner = runner

    def raw_bytes(self) -> bytes:
        """Return the raw source bytes (guarded by ``reads_raw_source``)."""
        if not self.permissions.reads_raw_source:
            raise PermissionDenied(
                f"capability {self.capability_id!r} does not declare "
                "reads_raw_source: raw source is not materialised for it"
            )
        return self._runner.source_bytes

    def model(self, role: str) -> Any:
        """Return the model client for ``role`` (guarded by ``calls_model``)."""
        granted = self.permissions.calls_model
        if granted is None or granted != role:
            raise PermissionDenied(
                f"capability {self.capability_id!r} is not granted model "
                f"role {role!r}: model client withheld"
            )
        return self._runner.model_for(role)

    def record_checks(self, items: list[Mapping[str, Any]]) -> None:
        """Record verdict checks (guarded by ``writes_verdict``).

        Each item declares ``check_id``, ``kind``, ``passed``, ``measured``,
        ``threshold`` and ``threshold_source``. The engine binds
        ``artifact_sha256`` to the current attempt artifact and ``verifier``
        to the calling capability; undeclared check ids are rejected.
        """
        if not self.permissions.writes_verdict:
            raise PermissionDenied(
                f"capability {self.capability_id!r} does not declare "
                "writes_verdict: it cannot produce check results"
            )
        declared = {entry["id"]: entry for entry in self.manifest.checks}
        current = self._runner.current_artifact_sha
        if current is None:
            raise RunnerError(
                f"capability {self.capability_id!r} recorded checks with no "
                "artifact bound on this attempt"
            )
        for item in items:
            if not isinstance(item, Mapping):
                raise RunnerError("check items must be mappings")
            check_id = item.get("check_id")
            if check_id not in declared:
                raise RunnerError(
                    f"capability {self.capability_id!r} recorded undeclared "
                    f"check {check_id!r}"
                )
            for key in ("kind", "passed", "threshold_source"):
                if key not in item:
                    raise RunnerError(
                        f"check {check_id!r} is missing {key!r}"
                    )
            if not isinstance(item["passed"], bool):
                raise RunnerError(
                    f"check {check_id!r}: 'passed' must be true or false"
                )
            position = item.get("band_position")
            if position is not None and (
                isinstance(position, bool)
                or not isinstance(position, (int, float))
            ):
                raise RunnerError(
                    f"check {check_id!r}: 'band_position' must be a number"
                )
            self._runner.pending_checks.append(
                EngineCheckResult(
                    check_id=str(check_id),
                    kind=str(item["kind"]),
                    passed=bool(item["passed"]),
                    measured=item.get("measured"),
                    threshold=item.get("threshold"),
                    threshold_source=str(item["threshold_source"]),
                    artifact_sha256=current,
                    verifier=self.capability_id,
                    detail=str(item.get("detail", "")),
                    band_position=position,
                )
            )

    def record_advisory(self, name: str, payload: Any) -> None:
        """Log an advisory output; it never affects pass/fail."""
        self._runner.advisories.append(
            {"capability": self.capability_id, "name": name, "payload": payload}
        )
        self.event(
            f"{self.capability_id}:advisory",
            "ok",
            {"name": name, "payload": payload},
        )

    def record_artifact(
        self, name: str, media_type: str, payload: bytes
    ) -> Artifact:
        """Store one byte payload; identity is its SHA-256."""
        return self._runner.store_artifact(
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
        return self._runner.log.append(
            stage,
            status,
            dict(details or {}),
            usage=usage,
            artifact_sha256=artifact_sha256,
        )

    def flush_log(self, path: str | Path) -> Path:
        """Write the run log to ``path`` as JSON lines."""
        return self._runner.log.write_jsonl(path)

    @property
    def attempt_number(self) -> int:
        """1-based attempt inside a loop body; 0 outside any loop."""
        return self._runner.loop_attempt_no

    @property
    def source_path(self) -> Path:
        """Filesystem path of the profile source input."""
        return self._runner.source

    @property
    def images_dir(self) -> Path | None:
        """Optional directory of companion source files (``None`` unset)."""
        return self._runner.images_dir

    @property
    def local_critic_enabled(self) -> bool:
        """Whether the optional local text-critique step should run."""
        return self._runner.local_critic_enabled

    @property
    def out_dir(self) -> Path:
        """Base output directory for run artifacts."""
        return self._runner.out

    def artifact_payload(self, sha256: str) -> bytes:
        """Return stored bytes for one recorded artifact hash."""
        try:
            return self._runner.artifact_payloads[sha256]
        except KeyError as exc:
            raise RunnerError(
                f"no stored payload for artifact {sha256}"
            ) from exc

    def provenance_text(self) -> str:
        """Render the release provenance statement from run state."""
        from ..artifacts.package import build_provenance as _build

        return _build(
            source_name=str(self.state.get("source_name", "")),
            source_sha256=str(self.state.get("source_sha256", "")),
            licence=self.state.get("licence"),
            attribution=self.state.get("attribution"),
            artifact_sha256=str(self.state.get("artifact_sha256", "")),
            plan=self.state.get("plan") or {"seed": 0, "n_rows": 0},
            verdict=str(self.state.get("verdict", "")),
            run_id=self._runner.log.run_id,
        )

    def pack_release(self, artifact_dir: str | Path) -> tuple[Path, str]:
        """Write the reproducible release ZIP for an artifact directory."""
        from ..artifacts.package import write_release_zip as _pack

        return _pack(Path(artifact_dir))

    def say(self, message: str) -> None:
        """Print one line of run progress."""
        self._runner.say(message)


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
    ) -> None:
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError(
                f"max_attempts must be an integer, got {max_attempts!r}"
            )
        if max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")
        missing = [c for c in manifest.capabilities if c not in registry]
        if missing:
            raise RunnerError(
                f"registry is missing capabilities: {', '.join(missing)}"
            )
        self.manifest = manifest
        self.registry = dict(registry)
        self.source = source
        if images_dir is not None:
            images_dir = Path(images_dir)
            if not images_dir.is_dir():
                raise RunnerError(
                    f"images directory not found: {images_dir}"
                )
        self.images_dir = images_dir
        self.local_critic_enabled = bool(local_critic)
        self.max_attempts = max_attempts
        self.model_factory = model_factory
        self.out = out
        self._say = say
        self._ask = ask
        self.log = RunLog()
        self._in_loop = False
        self.state: dict[str, Any] = {}
        self.pending_checks: list[EngineCheckResult] = []
        self.advisories: list[dict[str, Any]] = []
        self.attempts: list[Attempt] = []
        self.artifact_payloads: dict[str, bytes] = {}
        self.current_artifact_sha: str | None = None
        self.attempt_artifacts: list[str] = []
        declared = manifest.models.get("roles", {})
        if not isinstance(declared, dict):
            declared = {}
        self.role_bindings: dict[str, dict[str, Any]] = {
            role: dict(config) if isinstance(config, Mapping) else {}
            for role, config in (
                role_bindings.items() if role_bindings is not None
                else declared.items()
            )
        }
        source_map = (
            role_binding_sources if role_binding_sources is not None else {}
        )
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
                **(
                    {"images_dir": str(images_dir)}
                    if images_dir is not None
                    else {}
                ),
                **({"local_critic": True} if local_critic else {}),
            },
            limits=dict(manifest.limits),
        )
        try:
            self.source_bytes = source.read_bytes()
        except OSError as exc:
            raise RunnerError(f"cannot read source {source}: {exc}") from exc
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

    def model_for(self, role: str) -> Any:
        """Resolve a model role to a client, enforcing availability."""
        if role in self._models:
            return self._models[role]
        if role not in self.role_bindings:
            raise PermissionDenied(
                f"model role {role!r} is not bound by the manifest"
            )
        config = self.role_bindings[role]
        if config.get("enabled", True) is False:
            raise PermissionDenied(
                f"model role {role!r} is disabled: treated as missing"
            )
        client = self.model_factory(role, dict(config))
        guarded = _BudgetedClient(client, self)
        self._models[role] = guarded
        return guarded

    def store_artifact(
        self, name: str, media_type: str, payload: bytes, produced_by: str
    ) -> Artifact:
        from ..artifacts.hashing import sha256_bytes

        digest = sha256_bytes(payload)
        self.artifact_payloads[digest] = payload
        self.current_artifact_sha = digest
        self.attempt_artifacts.append(digest)
        record = Artifact(
            sha256=digest,
            media_type=media_type,
            produced_by=produced_by,
            attempt=len(self.attempts) + 1,
            name=name,
        )
        existing = self.state.setdefault("artifacts", [])
        existing.append(record)
        return record

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
            manifest=self.manifest,
            state=self.state,
            runner=self,
        )

    def run_capability(self, capability_id: str) -> None:
        self.registry[capability_id](self.scoped(capability_id))

    def enforce_limits(self) -> None:
        """Enforce manifest limits against the current plan, when present."""
        max_rows = self.manifest.limits.get("max_rows")
        plan = self.state.get("plan")
        if (
            isinstance(max_rows, int)
            and isinstance(plan, Mapping)
            and isinstance(plan.get("n_rows"), int)
            and not isinstance(plan.get("n_rows"), bool)
            and plan["n_rows"] > max_rows
        ):
            raise RunnerError(
                f"plan n_rows {plan['n_rows']} exceeds limit max_rows "
                f"{max_rows}"
            )

    def run_loop(self, node: Mapping[str, Any]) -> str:
        """Execute a loop node; return the final verdict."""
        body = list(node["loop"])
        on_fail = str(node["on_fail"])
        stage = str(node.get("stage", "loop"))
        bound = node.get(MAX_ATTEMPTS_BINDING)
        if bound != MAX_ATTEMPTS_BINDING:
            raise RunnerError(
                "loop nodes bind max_attempts to the caller supply only"
            )
        best: Attempt | None = None
        previous_failed: int | None = None
        self._in_loop = True
        try:
            for attempt_number in range(1, self.max_attempts + 1):
                self.pending_checks = []
                self.attempt_artifacts = []
                for capability_id in body:
                    self.run_capability(capability_id)
                self.enforce_limits()
                failed = [
                    check.check_id
                    for check in self.pending_checks
                    if not check.passed
                ]
                verdict = ATTEMPT_FAIL if failed else ATTEMPT_PASS
                attempt = Attempt(
                    n=attempt_number,
                    plan_ref=dict(self.state.get("plan") or {}),
                    artifact_refs=list(self.attempt_artifacts),
                    check_results=list(self.pending_checks),
                    status=verdict,
                )
                self.attempts.append(attempt)
                usages = self.state.get("attempt_usages", [])
                usage = usages[-1] if usages else None
                self.log.append(
                    stage,
                    "ok" if verdict == ATTEMPT_PASS else "fail",
                    {
                        "attempt": attempt_number,
                        "verdict": verdict,
                        "failed": failed,
                        "plan": dict(self.state.get("plan") or {}),
                    },
                    usage=usage,
                    artifact_sha256=(
                        self.attempt_artifacts[-1]
                        if self.attempt_artifacts
                        else None
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
                if (
                    previous_failed is not None
                    and len(failed) >= previous_failed
                ):
                    self.say(
                        f"[{stage}] no improvement across two attempts: stopping"
                    )
                    break
                previous_failed = len(failed)
                self.run_capability(on_fail)
        finally:
            self._in_loop = False
        assert best is not None
        self.state["attempts"] = [a.to_dict() for a in self.attempts]
        self.state["best_attempt"] = best.to_dict()
        self.state["verdict"] = best.status
        self.state["failed"] = list(best.failed)
        self.state["artifact_sha256"] = (
            best.artifact_refs[-1] if best.artifact_refs else None
        )
        return best.status

    def run_gate(self, gate_id: str) -> Decision:
        """Prompt the human and record the decision verbatim."""
        self.say("Human review required: the model cannot accept its own output.")
        try:
            decision = self._ask("Decision [accept/modify/reject]: ").strip().lower()
            note = self._ask("Note (recorded verbatim): ").rstrip("\r")
        except EOFError as exc:
            self.say(f"[{gate_id}] error: {exc}")
            raise RunnerError(f"no human decision for gate {gate_id!r}") from exc
        record = Decision(
            verdict=decision, note=note, decided_by="human"
        ).with_time()
        self.state["decision"] = {
            "verdict": record.verdict,
            "note": record.note,
            "decided_by": record.decided_by,
            "at": record.at,
        }
        self.log.append(
            gate_id,
            decision if decision in ("accept", "modify", "reject") else "invalid",
            {"decision": decision, "note": note},
            artifact_sha256=self.state.get("artifact_sha256"),
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
) -> tuple[int, dict[str, Any]]:
    """Execute a manifest graph; return (exit code, outcome summary).

    ``max_attempts`` is required and has no default: the caller supplies
    the bound and every loop node binds to it. ``local_critic`` enables
    the optional advisory text-critique step (default off); advisory
    output is logged and never affects the verdict. ``role_bindings``
    carries the composition root's effective per-role configs (after
    CLI/env/profile/default resolution); when omitted the manifest's own
    bindings apply.
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
                    "sources": dict(
                        runner.role_binding_sources.get(role, {})
                    ),
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
                if runner.state.get("verdict") != ATTEMPT_PASS:
                    return _refuse_gate(runner, gate_id)
                decision = runner.run_gate(gate_id)
                if runner.state.get("artifact_dir") is not None:
                    runner.log.write_jsonl(
                        Path(str(runner.state["artifact_dir"])) / "run.jsonl"
                    )
                if decision.verdict == "accept":
                    continue
                if decision.verdict in ("modify", "reject"):
                    runner.say(
                        f"[{gate_id}] {decision.verdict}: no release written"
                    )
                    return EXIT_DECLINED, _outcome(runner, "declined")
                runner.say(
                    f"[{gate_id}] invalid decision {decision.verdict!r}: "
                    "no release written"
                )
                return EXIT_DECLINED, _outcome(runner, "invalid")
    except PermissionDenied as exc:
        runner.say(f"[engine] permission denied: {exc}")
        return EXIT_FAILED, _outcome(runner, "error", str(exc))
    except RunnerError as exc:
        runner.say(f"[engine] error: {exc}")
        return EXIT_FAILED, _outcome(runner, "error", str(exc))
    except Exception as exc:  # noqa: BLE001 -- capability fault, logged
        runner.say(f"[engine] capability failed: {exc}")
        return EXIT_FAILED, _outcome(runner, "error", str(exc))
    return EXIT_OK, _outcome(runner, "released")


def _outcome(
    runner: _Runner, status: str, error: str | None = None
) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "status": status,
        "run_id": runner.run.id,
        "profile": runner.manifest.id,
        "verdict": runner.state.get("verdict"),
        "failed": list(runner.state.get("failed") or []),
        "artifact_sha256": runner.state.get("artifact_sha256"),
        "artifact_dir": runner.state.get("artifact_dir"),
        "attempts": len(runner.attempts),
        "advisories": list(runner.advisories),
    }
    if error is not None:
        outcome["error"] = error
    return outcome


def _refuse_gate(
    runner: _Runner, gate_id: str
) -> tuple[int, dict[str, Any]]:
    """Decline the gate on a failing verdict: nothing to accept.

    The artifact steps before the gate still run, so the failing evidence
    is stored; the human is never asked to accept a failing run.
    """
    failed: list[str] = list(runner.state.get("failed") or [])
    best = runner.state.get("best_attempt") or {}
    details = {
        check["check_id"]: check.get("detail", "")
        for check in best.get("check_results", [])
        if not check.get("passed", True)
    }
    artifact_dir = runner.state.get("artifact_dir")
    runner.say(f"[{gate_id}] skipped: validation failed, nothing to accept")
    for check_id in failed:
        runner.say(f"[fail] {check_id}")
    for check_id in failed:
        detail = details.get(check_id, "")
        if detail:
            runner.say(f"[fail] {check_id}: {detail}")
    if artifact_dir is not None:
        runner.log.append(
            gate_id,
            "skipped",
            {"reason": "failing verdict", "failed": failed},
            artifact_sha256=runner.state.get("artifact_sha256"),
        )
        runner.log.write_jsonl(Path(str(artifact_dir)) / "run.jsonl")
    return EXIT_FAILED, _outcome(runner, "failed")
