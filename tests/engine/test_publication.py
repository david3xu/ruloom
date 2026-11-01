"""Separate publication controls: ship accepted bytes, recover, deny.

Every ledger control case for the publication half of RP-09 lives
here: the submit-to-download chain over a real engine run, idempotent
re-publication, interrupted-publication recovery, cross-authority
denial, and refusal of foreign, mismatched, or unverifiable bytes.
The engine runs use the tabular profile with a replay double, so no
case needs a live model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from engine.artifacts import publication
from engine.artifacts.publication import PublicationError, Publisher, download
from engine.core.dispatch import AuthorityDenied, DispatchStore
from engine.core.manifest import load_manifest
from engine.core.runner import EXIT_OK, execute
from models.base import Completion, Usage
from profiles.tabular.capabilities import CAPABILITIES

REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = load_manifest(REPO_ROOT / "profiles" / "tabular" / "profile.json")

T0 = 2_000_000.0


class FakeModel:
    """A planner double replaying queued plans."""

    def __init__(self, plans: Sequence[Mapping[str, Any]]) -> None:
        self._plans = [dict(p) for p in plans]

    def complete(self, prompt: str, schema: Mapping[str, Any]) -> Completion:
        return Completion(
            data=dict(self._plans.pop(0)),
            usage=Usage(model="fake", input_tokens=1, output_tokens=1, calls=1),
            meta={"model": "fake"},
        )


def _write_source(path: Path) -> Path:
    csv_path = path / "sample-tabular.csv"
    csv_path.write_text("value,label\n1,A\n2,B\n3,A\n4,B\n", encoding="utf-8")
    (path / "sample-tabular-source.json").write_text(
        json.dumps(
            {
                "dataset_name": "publication-fixture",
                "licence": "CC-BY-4.0",
                "attribution": "Audit fixture.",
            }
        ),
        encoding="utf-8",
    )
    return csv_path


def _accepted_run(out: Path, source: Path) -> tuple[Path, dict[str, Any]]:
    """Run the engine to an accepted release; return (run_dir, outcome)."""
    factory = lambda role, config: FakeModel(  # noqa: E731
        [{"seed": 1, "n_rows": 100}, {"seed": 1, "n_rows": 100}]
    )
    answers = lambda prompt: (  # noqa: E731
        "accept" if prompt.startswith("Decision") else "ok"
    )
    code, outcome = execute(
        MANIFEST,
        CAPABILITIES,
        source=source,
        max_attempts=1,
        model_factory=factory,
        out=out,
        say=lambda message: None,
        ask=answers,
    )
    assert code == EXIT_OK
    run_dir = out / "runs" / outcome["run_id"]
    assert (run_dir / "release.zip").is_file()
    return run_dir, outcome


def _succeeded_item(
    base: Path, run_dir: Path, outcome: Mapping[str, Any], *, key: str = "k1"
) -> tuple[DispatchStore, dict[str, Any]]:
    store = DispatchStore(base)
    item = store.submit(
        authority="team-a",
        request={"profile": "tabular", "source": "sample-tabular.csv"},
        idempotency_key=key,
        now=T0,
    )
    lease = store.claim(
        item["item_id"], worker="w1", lease_seconds=600.0, now=T0
    )["lease"]
    zip_sha = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    return store, store.report_success(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        accepted_run={
            "run_id": outcome["run_id"],
            "artifact_sha256": outcome["artifact_sha256"],
            "zip_sha256": zip_sha,
        },
        now=T0,
    )


def _publisher(base: Path) -> Publisher:
    return Publisher(base, base / "published")


def test_publish_then_download_round_trip(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)

    identity = publisher.publish(
        item["item_id"], run_dir, authority="team-a", now=T0
    )
    published_zip = Path(identity["dest"])
    assert published_zip.is_file()
    assert identity["artifact_sha256"] == outcome["artifact_sha256"]
    reviewed = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    assert identity["zip_sha256"] == reviewed
    sidecar = published_zip.parent / f"{published_zip.name}.sha256"
    assert sidecar.read_text(encoding="utf-8") == f"{reviewed}  {published_zip.name}\n"

    fetched = download(published_zip, tmp_path / "fetch" / "release.zip")
    assert fetched["zip_sha256"] == reviewed
    assert fetched["artifact_sha256"] == outcome["artifact_sha256"]
    assert fetched["zip_sha256"] == identity["zip_sha256"]


def test_republish_is_a_noop(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)
    first = publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    before = Path(first["dest"]).read_bytes()
    second = publisher.publish(
        item["item_id"], run_dir, authority="team-a", now=T0 + 10.0
    )
    assert second == first
    assert Path(second["dest"]).read_bytes() == before


def test_interrupted_copy_resumes_to_same_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)

    def boom(dest_dir: Path, dest_name: str, payload: bytes) -> Path:
        staged = dest_dir / (dest_name + ".tmp.crash")
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(payload)
        raise RuntimeError("injected copy failure")

    monkeypatch.setattr(publication, "_stage_and_commit", boom)
    with pytest.raises(RuntimeError, match="injected copy failure"):
        publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    dest = base / "published" / f"{item['item_id']}.zip"
    assert not dest.exists()
    record = publisher.get(item["item_id"])
    assert record is not None and record["status"] == "in_progress"

    monkeypatch.undo()
    identity = publisher.publish(
        item["item_id"], run_dir, authority="team-a", now=T0 + 5.0
    )
    reviewed = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    assert identity["zip_sha256"] == reviewed
    assert publisher.get(item["item_id"])["status"] == "complete"
    assert list((base / "published").glob("*.tmp.*")) == []
    fetched = download(dest, tmp_path / "fetch" / "release.zip")
    assert fetched["zip_sha256"] == reviewed


def test_interrupted_mark_adopts_matching_bytes(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)
    identity = publisher.publish(
        item["item_id"], run_dir, authority="team-a", now=T0
    )
    # Simulate a crash after the copy but before the complete mark.
    record_path = base / "publications" / f"{item['item_id']}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["status"] = "in_progress"
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    resumed = publisher.publish(
        item["item_id"], run_dir, authority="team-a", now=T0 + 5.0
    )
    assert resumed == identity


def test_publish_denies_a_foreign_authority(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    with pytest.raises(AuthorityDenied):
        _publisher(base).publish(item["item_id"], run_dir, authority="team-b")
    assert not (base / "published" / f"{item['item_id']}.zip").exists()
    assert _publisher(base).get(item["item_id"]) is None


def test_publish_refuses_unsucceeded_or_mismatched_work(
    tmp_path: Path,
) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    store = DispatchStore(base)
    publisher = _publisher(base)

    claimed = store.submit(
        authority="team-a",
        request={"profile": "tabular"},
        idempotency_key="unreported",
        now=T0,
    )
    store.claim(claimed["item_id"], worker="w1", lease_seconds=600.0, now=T0)
    with pytest.raises(PublicationError, match="not succeeded"):
        publisher.publish(claimed["item_id"], run_dir, authority="team-a")

    other_dir, _ = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome, key="bound")
    with pytest.raises(PublicationError, match="not .*"):
        publisher.publish(item["item_id"], other_dir, authority="team-a")

    _, stale = _succeeded_item(base, run_dir, outcome, key="stale")
    (run_dir / "release.zip").write_bytes(
        (run_dir / "release.zip").read_bytes() + b"\x00"
    )
    with pytest.raises(PublicationError, match="changed since the success"):
        publisher.publish(stale["item_id"], run_dir, authority="team-a")


def test_publish_refuses_missing_or_broken_releases(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)

    (run_dir / "release.zip").unlink()
    with pytest.raises(PublicationError, match="no accepted release"):
        publisher.publish(item["item_id"], run_dir, authority="team-a")

    run_dir2, outcome2 = _accepted_run(base, _write_source(tmp_path))
    _, item2 = _succeeded_item(base, run_dir2, outcome2, key="broken")
    raw = bytearray((run_dir2 / "release.zip").read_bytes())
    raw[100] ^= 0xFF
    (run_dir2 / "release.zip").write_bytes(bytes(raw))
    with pytest.raises(PublicationError, match="consumer verification"):
        publisher.publish(item2["item_id"], run_dir2, authority="team-a")


def test_publish_never_overwrites_foreign_bytes(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    dest_dir = base / "published"
    dest_dir.mkdir(parents=True, exist_ok=True)
    (dest_dir / f"{item['item_id']}.zip").write_bytes(b"not our bundle")
    with pytest.raises(PublicationError, match="attributes to this item"):
        _publisher(base).publish(item["item_id"], run_dir, authority="team-a")
    assert (dest_dir / f"{item['item_id']}.zip").read_bytes() == b"not our bundle"


def test_one_item_ships_one_release(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    _, item = _succeeded_item(base, run_dir, outcome)
    publisher = _publisher(base)
    publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)
    record_path = base / "publications" / f"{item['item_id']}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["zip_sha256"] = "0" * 64
    record_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(PublicationError, match="already published"):
        publisher.publish(item["item_id"], run_dir, authority="team-a", now=T0)


def test_download_removes_unverifiable_bundles(tmp_path: Path) -> None:
    base = tmp_path / "out"
    run_dir, _ = _accepted_run(base, _write_source(tmp_path))
    broken = tmp_path / "broken.zip"
    raw = bytearray((run_dir / "release.zip").read_bytes())
    raw[100] ^= 0xFF
    broken.write_bytes(bytes(raw))
    target = tmp_path / "fetch" / "release.zip"
    with pytest.raises(PublicationError, match="consumer verification"):
        download(broken, target)
    assert not target.exists()


def test_end_to_end_submit_claim_run_report_publish_download(
    tmp_path: Path,
) -> None:
    base = tmp_path / "out"
    store = DispatchStore(base)
    item = store.submit(
        authority="e2e",
        request={
            "profile": "tabular",
            "source": "sample-tabular.csv",
            "max_attempts": 1,
        },
        idempotency_key="e2e-1",
        max_retries=1,
        now=T0,
    )
    lease = store.claim(
        item["item_id"], worker="launcher-1", lease_seconds=600.0, now=T0
    )["lease"]

    run_dir, outcome = _accepted_run(base, _write_source(tmp_path))
    zip_sha = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    item = store.report_success(
        item["item_id"],
        worker="launcher-1",
        token=lease["token"],
        accepted_run={
            "run_id": outcome["run_id"],
            "artifact_sha256": outcome["artifact_sha256"],
            "zip_sha256": zip_sha,
        },
        now=T0,
    )
    assert item["status"] == "succeeded"

    publisher = Publisher(base, base / "published")
    identity = publisher.publish(
        item["item_id"], run_dir, authority="e2e", now=T0
    )
    fetched = download(
        Path(identity["dest"]), tmp_path / "consumer" / "release.zip"
    )
    reviewed = (run_dir / "release.sha256").read_text(encoding="utf-8").split()[0]
    assert fetched["zip_sha256"] == reviewed == identity["zip_sha256"]
    assert fetched["artifact_sha256"] == outcome["artifact_sha256"]
    run_record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert fetched["artifact_sha256"] == run_record["artifact_sha256"]
