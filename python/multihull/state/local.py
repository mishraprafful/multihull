from __future__ import annotations

import fcntl
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from multihull.state.base import Floor, StateRecord, utcnow

DEFAULT_DIR = Path(".multihull")
MIGRATIONS = (
    """
    CREATE TABLE IF NOT EXISTS targets (
        service TEXT NOT NULL,
        provider TEXT NOT NULL,
        ref TEXT NOT NULL,
        image_digest TEXT,
        spec_hash TEXT NOT NULL DEFAULT '',
        last_status TEXT NOT NULL DEFAULT 'Unknown',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (service, provider)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS floors (
        service TEXT NOT NULL,
        provider TEXT NOT NULL,
        min_replicas INTEGER NOT NULL,
        pre_degraded_min INTEGER,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (service, provider)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot_versions (
        service TEXT PRIMARY KEY,
        version INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
)
SCHEMA_VERSION = len(MIGRATIONS)
COLUMNS = "service, provider, ref, image_digest, spec_hash, last_status, updated_at"
FLOOR_COLUMNS = "service, provider, min_replicas, pre_degraded_min, updated_at"


class LocalState:
    def __init__(self, path: str | Path = DEFAULT_DIR / "state.db") -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            migrate(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
        finally:
            conn.close()

    def get(self, service: str, provider: str) -> StateRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT {COLUMNS} FROM targets WHERE service = ? AND provider = ?",
                (service, provider),
            ).fetchone()
        return row_to_record(row) if row else None

    def put(self, record: StateRecord) -> None:
        record.updated_at = utcnow()
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO targets ({COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    record.service,
                    record.provider,
                    record.ref,
                    record.image_digest,
                    record.spec_hash,
                    record.last_status,
                    record.updated_at.isoformat(),
                ),
            )

    def delete(self, service: str, provider: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM targets WHERE service = ? AND provider = ?", (service, provider)
            )
            conn.execute(
                "DELETE FROM floors WHERE service = ? AND provider = ?", (service, provider)
            )

    def list(self, service: str | None = None) -> list[StateRecord]:
        with self._connect() as conn:
            if service is None:
                rows = conn.execute(
                    f"SELECT {COLUMNS} FROM targets ORDER BY service, provider"
                ).fetchall()
            else:
                rows = conn.execute(
                    f"SELECT {COLUMNS} FROM targets WHERE service = ? ORDER BY provider",
                    (service,),
                ).fetchall()
        return [row_to_record(row) for row in rows]

    def list_floors(self, service: str) -> list[Floor]:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT {FLOOR_COLUMNS} FROM floors WHERE service = ? ORDER BY provider",
                (service,),
            ).fetchall()
        return [row_to_floor(row) for row in rows]

    def put_floor(self, floor: Floor) -> None:
        floor.updated_at = utcnow()
        with self._connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO floors ({FLOOR_COLUMNS}) VALUES (?, ?, ?, ?, ?)",
                (
                    floor.service,
                    floor.provider,
                    floor.min_replicas,
                    floor.pre_degraded_min,
                    floor.updated_at.isoformat(),
                ),
            )

    def delete_floor(self, service: str, provider: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM floors WHERE service = ? AND provider = ?", (service, provider)
            )

    def snapshot_version(self, service: str) -> int:
        with self._connect() as conn:
            return stored_snapshot_version(conn, service)

    def advance_snapshot_version(self, service: str, at_least: int) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                version = max(stored_snapshot_version(conn, service) + 1, at_least)
                conn.execute(
                    "INSERT OR REPLACE INTO snapshot_versions (service, version, updated_at) "
                    "VALUES (?, ?, ?)",
                    (service, version, utcnow().isoformat()),
                )
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        return version

    @contextmanager
    def lock(self) -> Iterator[None]:
        with self.lock_path.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def row_to_record(row: tuple) -> StateRecord:
    service, provider, ref, image_digest, spec_hash, last_status, updated_at = row
    return StateRecord(
        service=service,
        provider=provider,
        ref=ref,
        image_digest=image_digest,
        spec_hash=spec_hash,
        last_status=last_status,
        updated_at=datetime.fromisoformat(updated_at),
    )


def row_to_floor(row: tuple) -> Floor:
    service, provider, min_replicas, pre_degraded_min, updated_at = row
    return Floor(
        service=service,
        provider=provider,
        min_replicas=min_replicas,
        pre_degraded_min=pre_degraded_min,
        updated_at=datetime.fromisoformat(updated_at),
    )


def stored_snapshot_version(conn: sqlite3.Connection, service: str) -> int:
    row = conn.execute(
        "SELECT version FROM snapshot_versions WHERE service = ?", (service,)
    ).fetchone()
    return row[0] if row else 0


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> None:
    if schema_version(conn) >= SCHEMA_VERSION:
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        for statement in MIGRATIONS[schema_version(conn) :]:
            conn.execute(statement)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
