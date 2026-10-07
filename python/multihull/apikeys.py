from __future__ import annotations

import os
import re
from pathlib import Path

import blake3

API_KEY_FORMAT = "hull_<id>_<secret>"
API_KEY_PATTERN = re.compile(r"hull_(?P<id>[A-Za-z0-9]+)_(?P<secret>[!-~]+)")


class ApiKeyError(ValueError):
    pass


def hash_input(key: str) -> str:
    match = API_KEY_PATTERN.fullmatch(key)
    if match is None:
        raise ApiKeyError(
            f"expected {API_KEY_FORMAT}: id of ASCII letters and digits, "
            "secret of printable ASCII without spaces"
        )
    return f"{match['id']}_{match['secret']}"


def hash_api_key(key: str) -> str:
    return blake3.blake3(hash_input(key).encode()).hexdigest()


def read_entries(source: str) -> list[tuple[str, str]]:
    kind, _, location = source.partition(":")
    if kind == "env":
        raw = os.environ.get(location, "")
        entries = [entry.strip() for entry in raw.split(",")]
        return [(f"entry {n}", entry) for n, entry in enumerate(entries, start=1) if entry]
    if kind == "file":
        path = Path(location)
        if not path.exists():
            return []
        lines = [line.strip() for line in path.read_text().splitlines()]
        return [(f"line {n}", line) for n, line in enumerate(lines, start=1) if line]
    raise ApiKeyError(f"unsupported api key source: {source}")


def load_api_keys(source: str) -> list[str]:
    keys: list[str] = []
    for position, key in read_entries(source):
        try:
            hash_input(key)
        except ApiKeyError as exc:
            raise ApiKeyError(f"route api key {position} of {source}: {exc}") from None
        keys.append(key)
    return keys


def api_key_hashes(source: str) -> list[str]:
    return sorted({hash_api_key(key) for key in load_api_keys(source)})
