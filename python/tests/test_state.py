from __future__ import annotations

import threading
from pathlib import Path

from multihull.state import LocalState, StateBackend, StateRecord


def test_roundtrip(tmp_path: Path) -> None:
    state = LocalState(tmp_path / ".multihull" / "state.db")
    assert isinstance(state, StateBackend)
    assert state.get("svc", "gke") is None
    record = StateRecord("svc", "gke", '{"a": 1}', "sha256:x", "hash1", "Ready")
    state.put(record)
    loaded = state.get("svc", "gke")
    assert loaded is not None
    assert (loaded.ref, loaded.image_digest, loaded.spec_hash, loaded.last_status) == (
        '{"a": 1}',
        "sha256:x",
        "hash1",
        "Ready",
    )
    assert loaded.updated_at.tzinfo is not None

    state.put(StateRecord("svc", "modal", "{}", None, "hash2"))
    state.put(StateRecord("other", "modal", "{}", None, "hash3"))
    assert [r.provider for r in state.list("svc")] == ["gke", "modal"]
    assert len(state.list()) == 3

    state.put(StateRecord("svc", "gke", "{}", None, "hash4", "Degraded"))
    assert state.get("svc", "gke").spec_hash == "hash4"

    state.delete("svc", "gke")
    assert state.get("svc", "gke") is None
    assert LocalState(tmp_path / ".multihull" / "state.db").get("svc", "modal") is not None


def test_lock_is_exclusive(tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    order: list[str] = []
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with state.lock():
            order.append("holder-in")
            holding.set()
            release.wait(timeout=5)
            order.append("holder-out")

    def waiter() -> None:
        holding.wait(timeout=5)
        with LocalState(tmp_path / "state.db").lock():
            order.append("waiter-in")

    threads = [threading.Thread(target=holder), threading.Thread(target=waiter)]
    for t in threads:
        t.start()
    holding.wait(timeout=5)
    release.set()
    for t in threads:
        t.join(timeout=5)
    assert order == ["holder-in", "holder-out", "waiter-in"]
