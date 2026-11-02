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
    store: DispatchStore,
    *,
    authority: str = "team-a",
    key: str | None = "k1",
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
        store.submit(authority="team-a", request=_request(), max_retries=-1, now=T0)
    with pytest.raises(DispatchError):
        store.submit(authority="team-a", request=_request(), max_retries=True, now=T0)
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
    # Expiry consumes the retry budget, so the redispatch needs one
    # retry to spend (CP-03: expiry counts against max_retries).
    item = _submit(store, max_retries=1)
    first = store.claim(item["item_id"], worker="w1", lease_seconds=60.0, now=T0)
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
    lease = store.claim(item["item_id"], worker="w1", lease_seconds=60.0, now=T0)[
        "lease"
    ]
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
    lease = store.claim(item["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
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
    assert (
        store.cancel(queued["item_id"], authority="team-a", now=T0)["status"]
        == "cancelled"
    )
    # Cancelling twice is a no-op returning the record.
    assert (
        store.cancel(queued["item_id"], authority="team-a", now=T0)["status"]
        == "cancelled"
    )

    leased = _submit(store, key="l")
    store.claim(leased["item_id"], worker="w1", lease_seconds=600.0, now=T0)
    done = store.cancel(leased["item_id"], authority="team-a", now=T0)
    assert done["status"] == "cancelled"
    assert done["lease"] is None

    terminal = _submit(store, key="t")
    lease = store.claim(terminal["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
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
    # The recovery sweep consumes the retry budget (CP-03), so the
    # dead item needs one retry to spend for the requeue.
    dead = _submit(store, key="dead", max_retries=1)
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
    lease = store.claim(item["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
    requeued = store.report_failure(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        error="boom",
        now=T0,
    )
    assert requeued["status"] == "queued"
    assert requeued["retries_used"] == 1
    lease2 = store.claim(item["item_id"], worker="w2", lease_seconds=600.0, now=T0)[
        "lease"
    ]
    assert lease2["token"] == lease["token"] + 1
    spent = store.report_failure(
        item["item_id"],
        worker="w2",
        token=lease2["token"],
        error="boom again",
        now=T0,
    )
    assert spent["status"] == "exhausted"
    assert spent["retries_used"] == 2
    with pytest.raises(ClaimConflict):
        store.claim(item["item_id"], worker="w3", lease_seconds=60.0, now=T0)


def test_zero_retries_exhausts_on_first_failure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _submit(store)
    lease = store.claim(item["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
    spent = store.report_failure(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        error="boom",
        now=T0,
    )
    assert spent["status"] == "exhausted"


def test_report_success_binds_the_accepted_run(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _submit(store)
    lease = store.claim(item["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
    accepted = {
        "run_id": "abc123",
        "artifact_sha256": "a" * 64,
        "zip_sha256": "b" * 64,
    }
    done = store.report_success(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        accepted_run=accepted,
        now=T0,
    )
    assert done["status"] == "succeeded"
    assert done["accepted_run"] == accepted
    assert done["lease"] is None
    with pytest.raises(DispatchError):
        store.report_success(
            item["item_id"],
            worker="w1",
            token=lease["token"],
            accepted_run={},
            now=T0,
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
    assert [item["item_id"] for item in store.list_items(status="cancelled")] == [
        first["item_id"]
    ]


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
    lease = store.claim(first["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
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
        _explicit(store, request=_request(max_attempts=2))
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
        lambda: store.claim(
            f"no-such-{bad_id}", worker="w1", lease_seconds=60.0, now=T0
        ),
        lambda: store.get(bad_id),
        lambda: store.cancel(bad_id, authority="team-a", now=T0),
        lambda: store.recover(bad_id, now=T0),
    ):
        with pytest.raises(DispatchError):
            call()
    with pytest.raises(DispatchError):
        store.report_success(
            bad_id,
            worker="w1",
            token=1,
            accepted_run={"run_id": "r", "artifact_sha256": "a", "zip_sha256": "z"},
            now=T0,
        )
    with pytest.raises(DispatchError):
        store.report_failure(
            bad_id,
            worker="w1",
            token=1,
            error="boom",
            now=T0,
        )
    with pytest.raises(DispatchError):
        store.heartbeat(
            bad_id,
            worker="w1",
            token=1,
            lease_seconds=60.0,
            now=T0,
        )
    escaped = tmp_path / "out" / "dispatch" / "escaped.json"
    assert not escaped.exists()
    assert sorted(
        path.name for path in (tmp_path / "out" / "dispatch" / "items").glob("*")
    ) == ["good-id.json"]


def test_expiry_reclaim_consumes_budget_and_exhausts(tmp_path: Path) -> None:
    """CP-03 A06: an expired-claim reclaim spends one retry, then exhausts."""
    from engine.core.dispatch import EXHAUSTED_BY_EXPIRY

    store = _store(tmp_path)
    item = store.submit(
        authority="team-a",
        request=_request(),
        max_retries=0,
        now=T0,
    )
    first = store.claim(item["item_id"], worker="w1", lease_seconds=60.0, now=T0)
    assert first["attempts"] == 1
    with pytest.raises(ClaimConflict):
        store.claim(item["item_id"], worker="w2", lease_seconds=60.0, now=T0 + 61.0)
    record = store.get(item["item_id"])
    assert record["status"] == "exhausted"
    assert record["retries_used"] == 1
    assert record["error"] == EXHAUSTED_BY_EXPIRY
    assert record["attempts"] == 1
    assert record["history"][-1]["event"] == "exhausted"
    assert record["history"][-1]["reason"] == EXHAUSTED_BY_EXPIRY
    with pytest.raises(ClaimConflict):
        store.claim(item["item_id"], worker="w3", lease_seconds=60.0, now=T0 + 62.0)


def test_recover_sweep_consumes_budget_and_exhausts(tmp_path: Path) -> None:
    """CP-03 A06: the recovery sweep spends one retry, then exhausts."""
    from engine.core.dispatch import EXHAUSTED_BY_EXPIRY

    store = _store(tmp_path)
    item = store.submit(
        authority="team-a",
        request=_request(),
        max_retries=0,
        now=T0,
    )
    store.claim(item["item_id"], worker="gone", lease_seconds=60.0, now=T0)
    recovered = store.recover(item["item_id"], now=T0 + 61.0)
    assert recovered["status"] == "exhausted"
    assert recovered["retries_used"] == 1
    assert recovered["error"] == EXHAUSTED_BY_EXPIRY
    assert recovered["history"][-1]["event"] == "exhausted"
    with pytest.raises(ClaimConflict):
        store.claim(item["item_id"], worker="fresh", lease_seconds=60.0, now=T0 + 61.0)


def test_repeated_expiry_cycles_cannot_requeue_forever(tmp_path: Path) -> None:
    """CP-03 A06: four claim/expire/recover cycles exhaust a budget of 2."""
    from engine.core.dispatch import EXHAUSTED_BY_EXPIRY

    store = _store(tmp_path)
    item = store.submit(
        authority="team-a",
        request=_request(),
        max_retries=2,
        now=T0,
    )
    base = T0
    # Cycle 1: claim then expire via reclaim (spends 1, relaunches).
    first = store.claim(item["item_id"], worker="w1", lease_seconds=60.0, now=base)
    assert first["attempts"] == 1
    second = store.claim(
        item["item_id"], worker="w2", lease_seconds=60.0, now=base + 61.0
    )
    assert second["status"] == "claimed"
    assert second["attempts"] == 2
    assert second["retries_used"] == 1
    assert second["lease"]["token"] == first["lease"]["token"] + 1
    # Cycle 2: expire via the recovery sweep (spends 1, back to queue).
    recovered = store.recover(item["item_id"], now=base + 122.0)
    assert recovered["status"] == "queued"
    assert recovered["retries_used"] == 2
    third = store.claim(
        item["item_id"], worker="w3", lease_seconds=60.0, now=base + 122.0
    )
    assert third["attempts"] == 3
    # Cycle 3+: the next expiry spends the last retry and exhausts.
    exhausted = store.recover(item["item_id"], now=base + 183.0)
    assert exhausted["status"] == "exhausted"
    assert exhausted["retries_used"] == 3
    assert exhausted["error"] == EXHAUSTED_BY_EXPIRY
    # A fourth cycle never requeues: the item is terminal.
    with pytest.raises(ClaimConflict):
        store.claim(item["item_id"], worker="w4", lease_seconds=60.0, now=base + 244.0)
    assert store.get(item["item_id"])["status"] == "exhausted"
    # The expired worker's token stays fenced off after exhaustion.
    with pytest.raises(FencingError):
        store.report_failure(
            item["item_id"],
            worker="w3",
            token=third["lease"]["token"],
            error="late",
            now=base + 244.0,
        )


def test_attempts_persisted_separately_from_failures(tmp_path: Path) -> None:
    """CP-03 A06: launches count in attempts; reports spend retries_used."""
    store = _store(tmp_path)
    item = store.submit(
        authority="team-a",
        request=_request(),
        max_retries=2,
        now=T0,
    )
    assert store.get(item["item_id"])["attempts"] == 0
    lease = store.claim(item["item_id"], worker="w1", lease_seconds=600.0, now=T0)[
        "lease"
    ]
    assert store.get(item["item_id"])["attempts"] == 1
    requeued = store.report_failure(
        item["item_id"],
        worker="w1",
        token=lease["token"],
        error="boom",
        now=T0,
    )
    assert requeued["attempts"] == 1
    assert requeued["retries_used"] == 1
    lease2 = store.claim(item["item_id"], worker="w2", lease_seconds=600.0, now=T0)[
        "lease"
    ]
    assert lease2["token"] == lease["token"] + 1
    record = store.get(item["item_id"])
    assert record["attempts"] == 2
    assert record["retries_used"] == 1


def test_submit_refuses_unknown_request_fields(tmp_path: Path) -> None:
    """DP-01 C01: unknown request fields are refused, never unenforced."""
    store = _store(tmp_path)
    with pytest.raises(DispatchError, match="unsupported request field"):
        store.submit(
            authority="team-a",
            request=_request(extra="changed"),
            now=T0,
        )
    with pytest.raises(DispatchError, match="unsupported request field"):
        store.submit(
            authority="team-a",
            request=_request(n_rows=9999),
            now=T0,
        )
    assert store.list_items() == []


def test_submit_validates_request_bounds_and_pins(tmp_path: Path) -> None:
    """DP-01 C01: malformed bounds and pins refuse at submit time."""
    store = _store(tmp_path)
    bad_requests = [
        _request(profile=""),
        _request(source=""),
        _request(max_attempts=0),
        _request(max_attempts=-1),
        _request(max_attempts=True),
        _request(max_attempts="1"),
        _request(max_rows=0),
        _request(max_rows=-2),
        _request(max_rows=False),
        _request(source_sha256="not-a-digest"),
        _request(source_sha256="0" * 63),
        _request(spec_digest="F" * 64),
        _request(spec=["limits"]),
        _request(spec={"limits": {"max_rows": "many"}}),
        _request(max_rows=5, spec={"limits": {"max_rows": 6}}),
    ]
    for bad in bad_requests:
        with pytest.raises(DispatchError):
            store.submit(authority="team-a", request=bad, now=T0)
    assert store.list_items() == []


def test_submit_persists_canonical_request_identity(tmp_path: Path) -> None:
    """DP-01 C01: submit persists the bound request identity."""
    store = _store(tmp_path)
    pinned = _request(
        source_sha256="a" * 64,
        spec_digest="b" * 64,
        spec={"limits": {"max_rows": 7}},
    )
    item = store.submit(authority="team-a", request=pinned, now=T0)
    assert item["request_identity"] == {
        "profile": "tabular",
        "source_sha256": "a" * 64,
        "spec_digest": "b" * 64,
    }
    assert item["request"] == pinned
    assert store.get(item["item_id"])["request_identity"] == item["request_identity"]


def test_submit_unpinned_request_carries_empty_identity(
    tmp_path: Path,
) -> None:
    """DP-01 C01: a request pinning nothing binds nothing shippable."""
    store = _store(tmp_path)
    item = store.submit(authority="team-a", request=_request(), now=T0)
    assert item["request_identity"] == {
        "profile": "tabular",
        "source_sha256": None,
        "spec_digest": None,
    }


def test_keyed_retry_identical_returns_same_item_unchanged(
    tmp_path: Path,
) -> None:
    """DP-02 C03(b): same key plus identical request returns one item.

    The retry returns the stored record unchanged: no new history
    event, no fencing-epoch move, no replacement record.
    """
    store = _store(tmp_path)
    first = store.submit(
        authority="team-a",
        request=_request(),
        idempotency_key="k",
        now=T0,
    )
    store.claim(first["item_id"], worker="w1", lease_seconds=600.0, now=T0)
    before = store.get(first["item_id"])
    second = store.submit(
        authority="team-a",
        request=_request(),
        idempotency_key="k",
        now=T0,
    )
    assert second == before
    assert second["history"] == before["history"]
    assert second["fencing"] == before["fencing"]
    assert second["status"] == "claimed"
    items = list((tmp_path / "out" / "dispatch" / "items").glob("*.json"))
    assert len(items) == 1


def test_keyed_retry_changed_request_refuses(tmp_path: Path) -> None:
    """DP-02 C03(c): same key with a different request is refused loudly.

    A changed source pin and a changed profile both mismatch the
    key-to-request binding persisted at first submission. The stored
    record keeps its history and fencing epoch.
    """
    store = _store(tmp_path)
    first = store.submit(
        authority="team-a",
        request=_request(),
        idempotency_key="k",
        now=T0,
    )
    with pytest.raises(DispatchError):
        store.submit(
            authority="team-a",
            request=_request(source_sha256="c" * 64),
            idempotency_key="k",
            now=T0,
        )
    with pytest.raises(DispatchError):
        store.submit(
            authority="team-a",
            request=_request(profile="hylogger"),
            idempotency_key="k",
            now=T0,
        )
    intact = store.get(first["item_id"])
    assert intact["request"] == _request()
    assert [entry["event"] for entry in intact["history"]] == ["submitted"]
    assert intact["fencing"] == 0
    items = list((tmp_path / "out" / "dispatch" / "items").glob("*.json"))
    assert len(items) == 1


def test_keyed_retry_matches_on_unenforced_fields_only(
    tmp_path: Path,
) -> None:
    """DP-02 C03(d) as repaired by HP-01 A01: key equivalence uses the
    enforced request binding, not the DP-01 request identity alone.

    Only fields nothing enforces (the source label) leave a retry
    identical: the same pins under the same key return the first item
    instead of refusing or duplicating. Caller bounds and the spec
    overlay are enforced, so changing them refuses (see the HP-01
    tests below).
    """
    store = _store(tmp_path)
    pinned = _request(source_sha256="a" * 64, spec_digest="b" * 64)
    first = store.submit(
        authority="team-a",
        request=pinned,
        idempotency_key="k",
        now=T0,
    )
    second = store.submit(
        authority="team-a",
        request=_request(
            source="renamed-source.csv",
            source_sha256="a" * 64,
            spec_digest="b" * 64,
        ),
        idempotency_key="k",
        now=T0,
    )
    assert second["item_id"] == first["item_id"]
    assert second["request"] == pinned
    items = list((tmp_path / "out" / "dispatch" / "items").glob("*.json"))
    assert len(items) == 1


def test_keyed_retry_changed_max_rows_refuses(tmp_path: Path) -> None:
    """HP-01 A01: same key with a changed row bound is refused loudly.

    A retry widening ``max_rows`` must never collapse onto the first
    item: the stored record keeps its request, history, and fencing
    epoch, and no second item is created.
    """
    store = _store(tmp_path)
    first = store.submit(
        authority="team-a",
        request=_request(max_rows=5),
        idempotency_key="k",
        now=T0,
    )
    with pytest.raises(DispatchError):
        store.submit(
            authority="team-a",
            request=_request(max_rows=6),
            idempotency_key="k",
            now=T0,
        )
    intact = store.get(first["item_id"])
    assert intact["request"] == _request(max_rows=5)
    assert [entry["event"] for entry in intact["history"]] == ["submitted"]
    assert intact["fencing"] == 0
    items = list((tmp_path / "out" / "dispatch" / "items").glob("*.json"))
    assert len(items) == 1


def test_keyed_retry_changed_attempt_bound_refuses(tmp_path: Path) -> None:
    """HP-01 A01: same key with a changed attempt bound refuses loudly."""
    store = _store(tmp_path)
    first = store.submit(
        authority="team-a",
        request=_request(),
        idempotency_key="k",
        now=T0,
    )
    with pytest.raises(DispatchError):
        store.submit(
            authority="team-a",
            request=_request(max_attempts=9),
            idempotency_key="k",
            now=T0,
        )
    intact = store.get(first["item_id"])
    assert intact["request"] == _request()
    assert [entry["event"] for entry in intact["history"]] == ["submitted"]
    assert intact["fencing"] == 0
    items = list((tmp_path / "out" / "dispatch" / "items").glob("*.json"))
    assert len(items) == 1


def test_keyed_retry_changed_spec_overlay_refuses(tmp_path: Path) -> None:
    """HP-01 A01: same key with a changed spec overlay refuses loudly.

    The overlay carries requested bounds (``spec.limits.max_rows``)
    that execution enforces, so a retry adding or widening it must
    never return the first item unchanged.
    """
    store = _store(tmp_path)
    first = store.submit(
        authority="team-a",
        request=_request(spec={"limits": {"max_rows": 7}}),
        idempotency_key="k",
        now=T0,
    )
    with pytest.raises(DispatchError):
        store.submit(
            authority="team-a",
            request=_request(spec={"limits": {"max_rows": 8}}),
            idempotency_key="k",
            now=T0,
        )
    with pytest.raises(DispatchError):
        store.submit(
            authority="team-a",
            request=_request(),
            idempotency_key="k",
            now=T0,
        )
    intact = store.get(first["item_id"])
    assert intact["request"] == _request(spec={"limits": {"max_rows": 7}})
    assert [entry["event"] for entry in intact["history"]] == ["submitted"]
    assert intact["fencing"] == 0
    items = list((tmp_path / "out" / "dispatch" / "items").glob("*.json"))
    assert len(items) == 1


def test_unkeyed_resubmission_matching_foreign_item_refuses(
    tmp_path: Path,
) -> None:
    """DP-02 C03(a): an unkeyed duplicate of another owner's job refuses.

    Owner B submits with an explicit key; owner A resubmits the same
    request body without the key. The resubmission is refused loudly
    instead of returning B's job, and B's record is untouched.
    """
    store = _store(tmp_path)
    foreign = store.submit(
        authority="team-b",
        request=_request(),
        idempotency_key="bk",
        now=T0,
    )
    with pytest.raises(AuthorityDenied):
        store.submit(authority="team-a", request=_request(), now=T0)
    intact = store.get(foreign["item_id"])
    assert intact["authority"] == "team-b"
    assert [entry["event"] for entry in intact["history"]] == ["submitted"]
    items = list((tmp_path / "out" / "dispatch" / "items").glob("*.json"))
    assert len(items) == 1
