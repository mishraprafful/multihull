from __future__ import annotations

from pathlib import Path

import pytest

from multihull.state import DEFAULT_STATE, LocalState, StateBackendError, open_state


def opened_path(url: str) -> Path:
    state = open_state(url)
    assert isinstance(state, LocalState)
    return state.path


def test_default_is_the_local_sqlite_file() -> None:
    assert DEFAULT_STATE == ".multihull/state.db"


def test_plain_paths_open_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert opened_path("nested/state.db") == Path("nested/state.db")
    assert (tmp_path / "nested" / "state.db").exists()
    absolute = tmp_path / "absolute.db"
    assert opened_path(str(absolute)) == absolute
    assert opened_path("./odd:name.db") == Path("odd:name.db")


def test_sqlite_urls_open_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    absolute = tmp_path / "with space" / "state.db"
    assert opened_path(f"sqlite://{tmp_path}/with%20space/state.db") == absolute
    assert absolute.exists()
    assert opened_path("sqlite:relative/state.db") == Path("relative/state.db")
    assert opened_path("SQLITE:upper.db") == Path("upper.db")
    assert (tmp_path / "relative" / "state.db").exists()


@pytest.mark.parametrize(
    ("url", "backend"),
    [
        ("s3://bucket/multihull/state.json", "S3"),
        ("gs://bucket/multihull/state.json", "GCS"),
        ("postgres://hull:hunter2@db.internal/multihull", "Postgres"),
        ("postgresql://hull:hunter2@db.internal/multihull", "Postgres"),
    ],
)
def test_planned_backends_are_refused_without_echoing_the_url(url: str, backend: str) -> None:
    with pytest.raises(StateBackendError) as raised:
        open_state(url)
    message = str(raised.value)
    assert f"the {backend} state backend" in message
    assert "planned but not implemented yet" in message
    assert "hunter2" not in message and "db.internal" not in message and "bucket" not in message


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("redis://cache:6379/0", "unsupported state backend scheme 'redis'"),
        ("file:///var/lib/multihull/state.db", "unsupported state backend scheme 'file'"),
        ("sqlite://host/state.db", "takes no host"),
        ("sqlite:///tmp/state.db?mode=ro", "no query or fragment"),
        ("sqlite:", "needs a file path"),
        ("   ", "the state backend is empty"),
    ],
)
def test_bad_urls_are_refused(url: str, reason: str) -> None:
    with pytest.raises(StateBackendError, match=reason):
        open_state(url)


def test_unopenable_sqlite_file_is_a_state_error(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    with pytest.raises(StateBackendError, match="cannot open SQLite state"):
        open_state(str(blocker / "state.db"))
