from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from multihull.state import Floor, LocalState, StateBackend, StateRecord

OLD_SCHEMA = """
CREATE TABLE targets (
    service TEXT NOT NULL,
    provider TEXT NOT NULL,
    ref TEXT NOT NULL,
    image_digest TEXT,
    spec_hash TEXT NOT NULL DEFAULT '',
    last_status TEXT NOT NULL DEFAULT 'Unknown',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (service, provider)
)
"""


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


def test_floors_roundtrip_and_go_with_their_target(tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    state.put(StateRecord("svc", "gke", "{}"))
    state.put_floor(Floor("svc", "gke", 3, 1))
    state.put_floor(Floor("svc", "modal", 1))
    state.put_floor(Floor("other", "gke", 2, 2))
    floors = {(f.provider, f.min_replicas, f.pre_degraded_min) for f in state.list_floors("svc")}
    assert floors == {("gke", 3, 1), ("modal", 1, None)}

    state.put_floor(Floor("svc", "gke", 2, 1))
    assert [f.min_replicas for f in state.list_floors("svc") if f.provider == "gke"] == [2]
    state.delete_floor("svc", "modal")
    state.delete("svc", "gke")
    assert state.list_floors("svc") == []
    assert len(state.list_floors("other")) == 1


def test_an_existing_state_file_is_migrated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute(OLD_SCHEMA)
        conn.execute(
            "INSERT INTO targets VALUES ('svc', 'gke', '{}', NULL, 'h', 'Ready', "
            "'2026-10-01T00:00:00+00:00')"
        )
    state = LocalState(path)
    assert state.get("svc", "gke").last_status == "Ready"
    state.put_floor(Floor("svc", "gke", 2, 1))
    assert [f.min_replicas for f in LocalState(path).list_floors("svc")] == [2]
    assert state.advance_snapshot_version("svc", 1) == 1
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_snapshot_versions_only_move_forward(tmp_path: Path) -> None:
    state = LocalState(tmp_path / "state.db")
    assert state.snapshot_version("svc") == 0
    assert state.advance_snapshot_version("svc", 1) == 1
    assert state.advance_snapshot_version("svc", 1) == 2
    assert state.advance_snapshot_version("svc", 40) == 40
    assert state.advance_snapshot_version("svc", 7) == 41
    assert state.snapshot_version("other") == 0
    state.delete("svc", "gke")
    assert LocalState(tmp_path / "state.db").snapshot_version("svc") == 41
