"""Controlled image admission, budgets, and recipient-evidence boundary checks."""

from __future__ import annotations

import dataclasses
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.artifacts.consumer import ReleaseVerificationError, verify_release
from engine.artifacts.hashing import sha256_bytes
from engine.artifacts.images import ImageSecurityError, staged_image_attachment
from engine.core.model_policy import ModelPolicyError
from engine.core.records import Artifact
from engine.core.runner import (
    BudgetExceeded,
    PermissionDenied,
    RunContext,
    _BudgetedClient,
    _Permissions,
    _Runner,
)
from models.base import Completion, ModelCapabilities, ModelError, Usage
from tests.models.test_image_input import _make_valid_png

CAPS = ModelCapabilities(
    input_modalities=("text", "image"),
    image_media_types=("image/png",),
    max_image_bytes=4096,
    max_image_pixels=100,
)


class Runner:
    def __init__(self, tmp: Path) -> None:
        self.source = tmp / "source.csv"
        self.source.write_bytes(b"source fixture")
        self.images_dir = None
        self._source_hashes = {sha256_bytes(self.source.read_bytes())}
        self.artifact_records = []
        self.artifact_payloads = {}
        self.manifest = SimpleNamespace(
            capabilities={
                "generate": {"reads_raw_source": False},
                "intake": {"reads_raw_source": True},
            }
        )
        self.model_budget = 30
        self.model_tokens = 0
        self.model_policy = "hosted"
        self.events = []
        self.log = SimpleNamespace(
            append=lambda stage, status, details: self.events.append(
                {"stage": stage, "status": status, "details": details}
            )
        )
        self.invocations = []

    @property
    def source_hashes(self):
        return frozenset(self._source_hashes)

    def check_model_policy(self, role):
        if self.model_policy == "offline":
            raise ModelPolicyError("offline")

    def record_model_usage(self, usage, **kwargs):
        self.model_tokens += usage.input_tokens + usage.output_tokens

    def note_model_call(self, usage):
        pass

    def note_model_invocation(self, role, client, completion, attached_info=None):
        self.invocations.append(attached_info)


class Client:
    capabilities = CAPS

    def __init__(self, failure=None, usage=None):
        self.calls = 0
        self.images = []
        self.failure = failure
        self.usage = usage or Usage(
            model="fixture", input_tokens=25, output_tokens=5, calls=1
        )

    def complete_with_image(self, prompt, schema, image_path):
        self.calls += 1
        self.images.append(Path(image_path).read_bytes())
        if self.failure:
            raise self.failure
        return Completion(
            {"note": "injected"},
            self.usage,
            {"provider": "fixture", "adapter": "injected", "evidence_kind": "injected"},
        )


def registered(tmp: Path):
    runner = Runner(tmp)
    payload = _make_valid_png()
    digest = sha256_bytes(payload)
    path = tmp / "generated.png"
    path.write_bytes(payload)
    runner.artifact_payloads[digest] = payload
    runner.artifact_records.append(Artifact(digest, "image/png", "generate", 1))
    return runner, path, payload, digest


@pytest.mark.parametrize(
    "fault",
    [
        "unregistered",
        "source_copy",
        "raw_producer",
        "unknown_producer",
        "mutated_registry",
        "wrong_media",
        "missing_record",
    ],
)
def test_provenance_refused_before_call(tmp_path, fault):
    runner, path, payload, digest = registered(tmp_path)
    if fault == "unregistered":
        runner.artifact_payloads.clear()
    if fault == "source_copy":
        runner._source_hashes.add(digest)
    if fault == "raw_producer":
        runner.artifact_records[-1] = dataclasses.replace(
            runner.artifact_records[-1], produced_by="intake"
        )
    if fault == "unknown_producer":
        runner.artifact_records[-1] = dataclasses.replace(
            runner.artifact_records[-1], produced_by="unknown"
        )
    if fault == "mutated_registry":
        runner.artifact_payloads[digest] = _make_valid_png(1, 1)
    if fault == "wrong_media":
        runner.artifact_records[-1] = dataclasses.replace(
            runner.artifact_records[-1], media_type="text/plain"
        )
    if fault == "missing_record":
        runner.artifact_records.clear()
    client = Client()
    with pytest.raises(ImageSecurityError):
        _BudgetedClient(client, runner, "critic").complete_with_image(
            "review", {}, path
        )
    assert client.calls == 0


def test_source_directory_and_symlink_refused(tmp_path):
    runner, path, _, _ = registered(tmp_path)
    runner.images_dir = tmp_path
    with pytest.raises(ImageSecurityError, match="source companion"):
        with staged_image_attachment(path, runner, CAPS):
            pass
    runner.images_dir = None
    link = tmp_path / "alias"
    link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ImageSecurityError, match="symbolic link"):
        with staged_image_attachment(link / path.name, runner, CAPS):
            pass


def test_stages_registered_snapshot_and_cleans_up(tmp_path):
    runner, path, payload, digest = registered(tmp_path)
    with staged_image_attachment(path, runner, CAPS) as attached:
        path.write_bytes(b"changed after admission")
        assert attached.staged_path.read_bytes() == payload
        assert attached.sha256 == digest
        assert attached.staged_path.stat().st_mode & 0o777 == 0o400
        assert attached.staged_path.parent.stat().st_mode & 0o777 == 0o700
    assert not attached.staged_path.exists()


@pytest.mark.parametrize(
    "caps",
    [
        dataclasses.replace(CAPS, max_image_bytes=20),
        dataclasses.replace(CAPS, max_image_pixels=1),
        ModelCapabilities(),
    ],
)
def test_limits_and_text_only_refuse_before_call(tmp_path, caps):
    runner, path, _, _ = registered(tmp_path)
    client = Client()
    client.capabilities = caps
    with pytest.raises(ModelError):
        _BudgetedClient(client, runner, "critic").complete_with_image(
            "review", {}, path
        )
    assert client.calls == 0


def test_usage_charged_and_next_call_refused(tmp_path):
    runner, path, payload, _ = registered(tmp_path)
    client = Client()
    guarded = _BudgetedClient(client, runner, "critic")
    guarded.complete_with_image("review", {}, path)
    assert client.images == [payload]
    assert runner.model_tokens == 30
    assert runner.invocations[0].sha256 == sha256_bytes(payload)
    with pytest.raises(BudgetExceeded):
        guarded.complete_with_image("review", {}, path)
    assert client.calls == 1


def test_offline_refused_before_call(tmp_path):
    runner, path, _, _ = registered(tmp_path)
    runner.model_policy = "offline"
    client = Client()
    with pytest.raises(ModelPolicyError):
        _BudgetedClient(client, runner, "critic").complete_with_image(
            "review", {}, path
        )
    assert client.calls == 0


@pytest.mark.parametrize(
    "failure,usage",
    [
        (ModelError("secret-provider-canary"), None),
        (None, Usage(model="fixture", input_tokens=-1, calls=1)),
    ],
)
def test_failed_call_usage_unknown_blocks_further_calls_and_redacts_log(
    tmp_path, failure, usage
):
    runner, path, _, _ = registered(tmp_path)
    client = Client(failure=failure, usage=usage)
    guarded = _BudgetedClient(client, runner, "critic")
    with pytest.raises(Exception):
        guarded.complete_with_image("review", {}, path)
    assert runner.events[-1]["details"]["usage"] is None
    assert "secret-provider-canary" not in json.dumps(runner.events)
    with pytest.raises(BudgetExceeded, match="unknown usage"):
        guarded.complete_with_image("review", {}, path)
    assert client.calls == 1


def test_source_registry_raw_grant_and_exact_digest(tmp_path):
    runner = Runner(tmp_path)
    ctx = RunContext(
        capability_id="generate", permissions=_Permissions(), engine=runner
    )
    with pytest.raises(PermissionDenied):
        ctx.register_source_artifact("a" * 64)
    with pytest.raises(Exception, match="64 lowercase"):
        _Runner.register_source_hash(runner, "not-a-digest")
    _Runner.register_source_hash(runner, "a" * 64)
    assert "a" * 64 in runner.source_hashes


def bundle_members():
    image = _make_valid_png()
    digest = sha256_bytes(image)
    facts = [{"check_id": "render_size", "status": "pass", "detail": "2x2"}]
    receipt = {
        "version": 1,
        "status": "ok",
        "artifact_sha256": digest,
        "deterministic_checks": facts,
        "model": "fixture",
        "provider": "fixture",
        "adapter": "injected",
        "evidence_kind": "injected",
        "usage": dataclasses.asdict(Usage(model="fixture", input_tokens=1, calls=1)),
        "cost": {"status": "unavailable", "reason": "injected fixture"},
        "observations": {"rendering": "No visible defect in fixture."},
        "note": "Injected advisory",
        "cited_check_ids": ["render_size"],
        "disclaimer": "Advisory only; no certification.",
    }
    data = b"value\n1\n"
    members = {
        "data.csv": data,
        "synthetic.png": image,
        "validation-report.json": json.dumps(
            {
                "artifact_name": "data.csv",
                "artifact_sha256": sha256_bytes(data),
                "verdict": "pass",
                "results": [],
            }
        ).encode(),
        "image-validation.json": json.dumps(
            {
                "artifact_name": "synthetic.png",
                "artifact_sha256": digest,
                "verdict": "pass",
                "results": [{"name": "render_size", "status": "pass", "detail": "2x2"}],
            }
        ).encode(),
    }
    return members, receipt


def write_bundle(path, members, receipt):
    unsigned = {k: v for k, v in receipt.items() if k != "receipt_sha256"}
    receipt["receipt_sha256"] = sha256_bytes(
        json.dumps(unsigned, sort_keys=True).encode()
    )
    members = dict(members, **{"vision-advisory.json": json.dumps(receipt).encode()})
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
        archive.writestr(
            "checksums.sha256",
            "".join(
                f"{sha256_bytes(payload)}  {name}\n"
                for name, payload in members.items()
            ),
        )


def test_recipient_validates_injected_advisory_binding(tmp_path):
    members, receipt = bundle_members()
    path = tmp_path / "release.zip"
    write_bundle(path, members, receipt)
    assert verify_release(path)["artifact_sha256"] == sha256_bytes(members["data.csv"])


@pytest.mark.parametrize(
    "fault",
    [
        "artifact",
        "facts",
        "status",
        "missing_version",
        "bad_usage",
        "unknown_citation",
        "missing_observations",
        "failure_fabricated_usage",
    ],
)
def test_recipient_refuses_invalid_receipt_even_with_recut_checksums(tmp_path, fault):
    members, receipt = bundle_members()
    if fault == "artifact":
        receipt["artifact_sha256"] = "a" * 64
    if fault == "facts":
        receipt["deterministic_checks"][0]["status"] = "fail"
    if fault == "status":
        receipt["status"] = "live_success_certified"
    if fault == "missing_version":
        del receipt["version"]
    if fault == "bad_usage":
        receipt["usage"]["input_tokens"] = -1
    if fault == "unknown_citation":
        receipt["cited_check_ids"] = ["unknown"]
    if fault == "missing_observations":
        receipt["observations"] = {}
    if fault == "failure_fabricated_usage":
        receipt["status"] = "unavailable"
    path = tmp_path / "release.zip"
    write_bundle(path, members, receipt)
    with pytest.raises(ReleaseVerificationError):
        verify_release(path)


def test_raw_record_cannot_be_relabelled_by_later_nonraw_registration(tmp_path):
    runner, path, _, digest = registered(tmp_path)
    runner.artifact_records.insert(0, Artifact(digest, "image/png", "intake", 1))
    with pytest.raises(ImageSecurityError, match="raw-intake"):
        with staged_image_attachment(path, runner, CAPS):
            pass


def test_failed_image_call_logs_whitelisted_reason_without_relaxing_guard(tmp_path):
    runner, path, _, _ = registered(tmp_path)
    failure = ModelError(
        "secret-provider-canary",
        reason_code="finish_reason_not_stop",
        diagnostics={
            "finish_reason": "length",
            "observed_model": "fixture-model",
            "reported_usage": {"prompt_tokens": 12, "completion_tokens": 4096},
            "content": "secret-provider-canary",
        },
    )
    client = Client(failure=failure)
    guarded = _BudgetedClient(client, runner, "critic")
    with pytest.raises(ModelError):
        guarded.complete_with_image("review", {}, path)
    details = runner.events[-1]["details"]
    assert details["reason_code"] == "finish_reason_not_stop"
    assert details["diagnostics"] == {
        "finish_reason": "length",
        "observed_model": "fixture-model",
        "reported_usage": {"prompt_tokens": 12, "completion_tokens": 4096},
    }
    # Diagnostics never become accounted usage or lift the unknown-usage guard.
    assert details["usage"] is None
    assert runner.model_tokens == 0
    assert "secret-provider-canary" not in json.dumps(runner.events)
    with pytest.raises(BudgetExceeded, match="unknown usage"):
        guarded.complete_with_image("review", {}, path)
    assert client.calls == 1


def test_forged_diagnostics_on_model_error_are_reduced_again(tmp_path):
    runner, path, _, _ = registered(tmp_path)
    failure = ModelError("x")
    failure.reason_code = "secret-provider-canary"
    failure.diagnostics = {"body": "secret-provider-canary", "finish_reason": "stop"}
    guarded = _BudgetedClient(Client(failure=failure), runner, "critic")
    with pytest.raises(ModelError):
        guarded.complete_with_image("review", {}, path)
    details = runner.events[-1]["details"]
    assert "reason_code" not in details
    assert details["diagnostics"] == {"finish_reason": "stop"}
    assert "secret-provider-canary" not in json.dumps(runner.events)


def test_non_model_error_contributes_no_diagnostics(tmp_path):
    runner, path, _, _ = registered(tmp_path)
    failure = RuntimeError("secret-provider-canary")
    failure.reason_code = "refusal"
    guarded = _BudgetedClient(Client(failure=failure), runner, "critic")
    with pytest.raises(RuntimeError):
        guarded.complete_with_image("review", {}, path)
    details = runner.events[-1]["details"]
    assert "reason_code" not in details and "diagnostics" not in details
