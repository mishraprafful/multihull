from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import unquote, urlsplit

from multihull.state.base import StateBackend
from multihull.state.local import DEFAULT_DIR, LocalState

STATE_ENV = "MULTIHULL_STATE_BACKEND"
DEFAULT_STATE = str(DEFAULT_DIR / "state.db")
SUPPORTED_FORMS = "a SQLite file path, sqlite:///absolute/path.db or sqlite:relative/path.db"
PLANNED_SCHEMES = {
    "s3": "S3",
    "gs": "GCS",
    "postgres": "Postgres",
    "postgresql": "Postgres",
}


class StateBackendError(ValueError):
    pass


def sqlite_path(url: str) -> Path:
    parts = urlsplit(url)
    if not parts.scheme:
        return Path(url)
    scheme = parts.scheme.lower()
    if scheme in PLANNED_SCHEMES:
        raise StateBackendError(
            f"the {PLANNED_SCHEMES[scheme]} state backend ({scheme}://) is planned but not "
            f"implemented yet; use {SUPPORTED_FORMS}"
        )
    if scheme != "sqlite":
        raise StateBackendError(
            f"unsupported state backend scheme {scheme!r}; use {SUPPORTED_FORMS}"
        )
    if parts.netloc:
        raise StateBackendError(
            "a sqlite state URL takes no host; use sqlite:///absolute/path.db "
            "or sqlite:relative/path.db"
        )
    if parts.query or parts.fragment:
        raise StateBackendError("a sqlite state URL takes no query or fragment")
    if not parts.path:
        raise StateBackendError("a sqlite state URL needs a file path")
    return Path(unquote(parts.path))


def open_state(url: str) -> StateBackend:
    if not url.strip():
        raise StateBackendError(f"the state backend is empty; use {SUPPORTED_FORMS}")
    path = sqlite_path(url)
    try:
        return LocalState(path)
    except (OSError, sqlite3.Error) as exc:
        raise StateBackendError(f"cannot open SQLite state at {path}: {exc}") from None
