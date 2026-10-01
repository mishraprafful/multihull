from __future__ import annotations

import fcntl
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from multihull.state.base import StateRecord, utcnow

DEFAULT_DIR = Path(".multihull")
SCHEMA = """
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
"""
COLUMNS = "service, provider, ref, image_digest, spec_hash, last_status, updated_at"


class LocalState:
    def __init__(self, path: str | Path = DEFAULT_DIR / "state.db") -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

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
