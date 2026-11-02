"""Durable dispatch controls: one winner, fenced leases, owned cancels.

Every ledger control case for the dispatch half of RP-09 lives here:
idempotent submit, single-winner claim, fencing of stale leases,
crash recovery after a dead launcher, cancellation, retry exhaustion,
and cross-authority denial. Clocks are injected (plain floats), so no
case sleeps or races the wall clock -- except the single-winner race
itself, which runs real threads.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from engine.core.dispatch import (
    AuthorityDenied,
    ClaimConflict,
    DispatchError,
    DispatchStore,
    FencingError,
    ItemNotFound,
)

T0 = 1_000_000.0


def _store(tmp_path: Path) -> DispatchStore:
    return DispatchStore(tmp_path / "out")


def _request(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "profile": "tabular",
        "source": "sample-tabular.csv",
        "max_attempts": 1,
    }
    base.update(over)
    return base


def _submit(
    store: DispatchStore, *, authority: str = "team-a", key: str | None = "k1",
    **over: Any,
) -> dict[str, Any]:
    return store.submit(
        authority=authority,
        request=_request(),
        idempotency_key=key,
        now=T0,
        **over,
    )


def test_submit_then_identical_resubmit_returns_one_item(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _submit(store)
    second = _submit(store)
    assert first["item_id"] == second["item_id"]
    assert second["status"] == "queued"
    items = list((tmp_path / "out" / "dispatch" / "items").glob("*.json"))
    assert len(items) == 1


def test_submit_without_key_creates_distinct_items(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _submit(store, key=None)
    second = _submit(store, key=None)
    assert first["item_id"] != second["item_id"]


def test_idempotency_key_belongs_to_first_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _submit(store, authority="team-a")
    with pytest.raises(AuthorityDenied):
        _submit(store, authority="team-b")


def test_submit_validates_inputs(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(DispatchError):
        store.submit(authority="  ", request=_request(), now=T0)
    with pytest.raises(DispatchError):
        store.submit(authority="team-a", request={}, now=T0)
    with pytest.raises(DispatchError):
        store.submit(
            authority="team-a", request=_request(), max_retries=-1, now=T0
        )
    with pytest.raises(DispatchError):
        store.submit(
            authority="team-a", request=_request(), max_retries=True, now=T0
        )
    with pytest.raises(DispatchError):
        store.submit(
            authority="team-a",
            request=_request(),
            idempotency_key="",
            now=T0,
        )


def test_single_winner_claim_under_threads(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _submit(store)
    winners: list[str] = []
    guard = threading.Lock()

    def race(worker: str) -> None:
        try:
            lease = store.claim(
                item["item_id"], worker=worker, lease_seconds=60.0, now=T0
            )
        except ClaimConflict:
            return
        with guard:
            winners.append(lease["lease"]["worker"])

    threads = [threading.Thread(target=race, args=(f"w{n}",)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(winners) == 1
    record = store.get(item["item_id"])
    assert record["status"] == "claimed"
    assert record["lease"]["worker"] == winners[0]


def test_claim_live_lease_conflicts_expired_lease_redispatches(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    item = _submit(store)
    first = store.claim(
        item["item_id"], worker="w1", lease_seconds=60.0, now=T0
    )
    with pytest.raises(ClaimConflict):
        store.claim(item["item_id"], worker="w2", lease_seconds=60.0, now=T0)
    second = store.claim(
        item["item_id"], worker="w2", lease_seconds=60.0, now=T0 + 61.0
    )
    assert second["lease"]["token"] == first["lease"]["token"] + 1
    assert second["lease"]["worker"] == "w2"
    events = [entry["event"] for entry in second["history"]]
    assert "lease_expired" in events


def test_claim_terminal_item_refuses(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _submit(store)
    store.cancel(item["item_id"], authority="team-a", now=T0)
    with pytest.raises(ClaimConflict):
        store.claim(item["item_id"], worker="w1", lease_seconds=60.0, now=T0)


def test_claim_validates_worker_and_bound(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _submit(store)
    with pytest.raises(DispatchError):
        store.claim(item["item_id"], worker=" ", lease_seconds=60.0, now=T0)
    with pytest.raises(DispatchError):
        store.claim(item["item_id"], worker="w1", lease_seconds=0, now=T0)
    with pytest.raises(ItemNotFound):
        store.claim("missing", worker="w1", lease_seconds=60.0, now=T0)


def test_heartbeat_extends_live_lease_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _submit(store)
    lease = store.claim(
        item["item_id"], worker="w1", lease_seconds=60.0, now=T0
    )["lease"]
    moved = store.heartbeat(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        lease_seconds=60.0,
        now=T0 + 30.0,
    )
    assert moved["lease"]["expires_at"] == T0 + 90.0
    with pytest.raises(FencingError):
        store.heartbeat(
            item["item_id"],
            worker="w2",
            token=lease["token"],
            lease_seconds=60.0,
            now=T0 + 30.0,
        )
    with pytest.raises(FencingError):
        store.heartbeat(
            item["item_id"],
            worker="w1",
            token=lease["token"] + 999,
            lease_seconds=60.0,
            now=T0 + 30.0,
        )
    with pytest.raises(FencingError):
        store.heartbeat(
            item["item_id"],
            worker="w1",
            token=lease["token"],
            lease_seconds=60.0,
            now=T0 + 90.0,
        )


def test_cancel_fences_off_inflight_worker(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _submit(store)
    lease = store.claim(
        item["item_id"], worker="w1", lease_seconds=600.0, now=T0
    )["lease"]
    store.cancel(item["item_id"], authority="team-a", now=T0 + 1.0)
    with pytest.raises(FencingError):
        store.report_success(
            item["item_id"],
            worker="w1",
            token=lease["token"],
            accepted_run={
                "run_id": "r",
                "artifact_sha256": "a",
                "zip_sha256": "z",
            },
            now=T0 + 2.0,
        )
    with pytest.raises(FencingError):
        store.heartbeat(
            item["item_id"],
            worker="w1",
            token=lease["token"],
            lease_seconds=60.0,
            now=T0 + 2.0,
        )


def test_cancel_needs_the_owning_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _submit(store, authority="team-a")
    with pytest.raises(AuthorityDenied):
        store.cancel(item["item_id"], authority="team-b", now=T0)
    assert store.get(item["item_id"])["status"] == "queued"


def test_cancel_shapes_queued_claimed_done_and_terminal(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    queued = _submit(store, key="q")
    assert store.cancel(queued["item_id"], authority="team-a", now=T0)[
        "status"
    ] == "cancelled"
    # Cancelling twice is a no-op returning the record.
    assert store.cancel(queued["item_id"], authority="team-a", now=T0)[
        "status"
    ] == "cancelled"

    leased = _submit(store, key="l")
    store.claim(leased["item_id"], worker="w1", lease_seconds=600.0, now=T0)
    done = store.cancel(leased["item_id"], authority="team-a", now=T0)
    assert done["status"] == "cancelled"
    assert done["lease"] is None

    terminal = _submit(store, key="t")
    lease = store.claim(
        terminal["item_id"], worker="w1", lease_seconds=600.0, now=T0
    )["lease"]
    store.report_success(
        terminal["item_id"],
        worker="w1",
        token=lease["token"],
        accepted_run={
            "run_id": "r",
            "artifact_sha256": "a",
            "zip_sha256": "z",
        },
        now=T0,
    )
    with pytest.raises(DispatchError):
        store.cancel(terminal["item_id"], authority="team-a", now=T0)


def test_crash_recovery_returns_expired_leases_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    dead = _submit(store, key="dead")
    store.claim(dead["item_id"], worker="gone", lease_seconds=60.0, now=T0)
    live = _submit(store, key="live")
    store.claim(live["item_id"], worker="here", lease_seconds=600.0, now=T0)
    idle = _submit(store, key="idle")

    recovered = store.recover(dead["item_id"], now=T0 + 61.0)
    assert recovered["status"] == "queued"
    assert recovered["lease"] is None
    assert recovered["history"][-1]["event"] == "recovered"

    assert store.recover(live["item_id"], now=T0 + 61.0)["status"] == "claimed"
    assert store.recover(idle["item_id"], now=T0 + 61.0)["status"] == "queued"
    # The recovered item is claimable again with a moved-on token.
    reclaim = store.claim(
        dead["item_id"], worker="fresh", lease_seconds=60.0, now=T0 + 61.0
    )
    assert reclaim["lease"]["token"] == 2


def test_retry_exhaustion_rests_the_item(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = store.submit(
        authority="team-a",
        request=_request(),
        idempotency_key="retry",
        max_retries=1,
        now=T0,
    )
    lease = store.claim(
        item["item_id"], worker="w1", lease_seconds=600.0, now=T0
    )["lease"]
    requeued = store.report_failure(
        item["item_id"], worker="w1", token=lease["token"],
        error="boom", now=T0,
    )
    assert requeued["status"] == "queued"
    assert requeued["retries_used"] == 1
    lease2 = store.claim(
        item["item_id"], worker="w2", lease_seconds=600.0, now=T0
    )["lease"]
    assert lease2["token"] == lease["token"] + 1
    spent = store.report_failure(
        item["item_id"], worker="w2", token=lease2["token"],
        error="boom again", now=T0,
    )
    assert spent["status"] == "exhausted"
    assert spent["retries_used"] == 2
    with pytest.raises(ClaimConflict):
        store.claim(item["item_id"], worker="w3", lease_seconds=60.0, now=T0)


def test_zero_retries_exhausts_on_first_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _submit(store)
    lease = store.claim(
        item["item_id"], worker="w1", lease_seconds=600.0, now=T0
    )["lease"]
    spent = store.report_failure(
        item["item_id"], worker="w1", token=lease["token"],
        error="boom", now=T0,
    )
    assert spent["status"] == "exhausted"


def test_report_success_binds_the_accepted_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _submit(store)
    lease = store.claim(
        item["item_id"], worker="w1", lease_seconds=600.0, now=T0
    )["lease"]
    accepted = {
        "run_id": "abc123",
        "artifact_sha256": "a" * 64,
        "zip_sha256": "b" * 64,
    }
    done = store.report_success(
        item["item_id"], worker="w1", token=lease["token"],
        accepted_run=accepted, now=T0,
    )
    assert done["status"] == "succeeded"
    assert done["accepted_run"] == accepted
    assert done["lease"] is None
    with pytest.raises(DispatchError):
        store.report_success(
            item["item_id"], worker="w1", token=lease["token"],
            accepted_run={}, now=T0,
        )


def test_records_are_copies_and_list_filters(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _submit(store, key="a")
    second = _submit(store, key="b")
    store.cancel(first["item_id"], authority="team-a", now=T0)
    probe = store.get(second["item_id"])
    probe["status"] = "mutated"
    assert store.get(second["item_id"])["status"] == "queued"
    assert [item["item_id"] for item in store.list_items()] == sorted(
        [first["item_id"], second["item_id"]]
    )
    assert [
        item["item_id"] for item in store.list_items(status="cancelled")
    ] == [first["item_id"]]


def _explicit(
    store: DispatchStore,
    *,
    authority: str = "team-a",
    request: dict[str, Any] | None = None,
    item_id: str = "fixed-id",
    now: float = T0,
) -> dict[str, Any]:
    return store.submit(
        authority=authority,
        request=dict(request) if request is not None else _request(),
        item_id=item_id,
        now=now,
    )


def test_explicit_id_resubmission_returns_the_immutable_record(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _explicit(store)
    lease = store.claim(
        first["item_id"], worker="w1", lease_seconds=600.0, now=T0
    )["lease"]
    store.report_success(
        first["item_id"],
        worker="w1",
        token=lease["token"],
        accepted_run={
            "run_id": "r",
            "artifact_sha256": "a",
            "zip_sha256": "z",
        },
        now=T0,
    )
    before = store.get("fixed-id")
    second = _explicit(store)
    assert second == before
    assert second["history"] == before["history"]
    assert second["fencing"] == before["fencing"]
    assert second["status"] == "succeeded"
    items = list((tmp_path / "out" / "dispatch" / "items").glob("*.json"))
    assert len(items) == 1


def test_explicit_id_same_authority_changed_request_refuses(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _explicit(store)
    store.claim(first["item_id"], worker="w1", lease_seconds=600.0, now=T0)
    with pytest.raises(DispatchError):
        _explicit(store, request=_request(extra="changed"))
    intact = store.get("fixed-id")
    assert intact["request"] == _request()
    assert intact["status"] == "claimed"
    assert [entry["event"] for entry in intact["history"]] == [
        "submitted",
        "claimed",
    ]


def test_explicit_id_foreign_authority_refuses_and_keeps_history(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first = _explicit(store, authority="owner-a")
    store.claim(first["item_id"], worker="w1", lease_seconds=30.0, now=T0)
    store.cancel(first["item_id"], authority="owner-a", now=T0 + 1.0)
    before = store.get("fixed-id")
    with pytest.raises(AuthorityDenied):
        _explicit(store, authority="owner-b")
    after = store.get("fixed-id")
    assert after == before
    assert after["authority"] == "owner-a"
    assert after["status"] == "cancelled"
    assert len(after["history"]) == len(before["history"]) == 3
    assert after["fencing"] == before["fencing"] == 2


def test_explicit_id_keyed_collision_with_foreign_item_refuses(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _explicit(store, authority="team-a")
    with pytest.raises(AuthorityDenied):
        store.submit(
            authority="team-b",
            request=_request(),
            idempotency_key="fresh-key",
            item_id="fixed-id",
            now=T0,
        )
    intact = store.get("fixed-id")
    assert intact["authority"] == "team-a"
    assert len(intact["history"]) == 1


@pytest.mark.parametrize(
    "bad_id",
    [
        "../../escaped",
        "..\\escaped",
        "/absolute",
        "a/b",
        "a\\b",
        ".",
        "..",
        "has space",
        "",
        "x" * 65,
        "trailing-dot.",
    ],
)
def test_item_id_rejects_traversal_and_shape_variants(
    tmp_path: Path, bad_id: str
) -> None:
    store = _store(tmp_path)
    with pytest.raises(DispatchError):
        store.submit(authority="team-a", request=_request(), item_id=bad_id)
    _explicit(store, item_id="good-id")
    for call in (
        lambda: store.claim(f"no-such-{bad_id}", worker="w1",
                            lease_seconds=60.0, now=T0),
        lambda: store.get(bad_id),
        lambda: store.cancel(bad_id, authority="team-a", now=T0),
        lambda: store.recover(bad_id, now=T0),
    ):
        with pytest.raises(DispatchError):
            call()
    with pytest.raises(DispatchError):
        store.report_success(
            bad_id, worker="w1", token=1,
            accepted_run={"run_id": "r", "artifact_sha256": "a",
                          "zip_sha256": "z"}, now=T0,
        )
    with pytest.raises(DispatchError):
        store.report_failure(
            bad_id, worker="w1", token=1, error="boom", now=T0,
        )
    with pytest.raises(DispatchError):
        store.heartbeat(
            bad_id, worker="w1", token=1, lease_seconds=60.0, now=T0,
        )
    escaped = tmp_path / "out" / "dispatch" / "escaped.json"
    assert not escaped.exists()
    assert sorted(
        path.name for path in (tmp_path / "out" / "dispatch" / "items").glob("*")
    ) == ["good-id.json"]
