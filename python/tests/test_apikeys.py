from __future__ import annotations

import json
from pathlib import Path

import pytest

from multihull import apikeys
from multihull.apikeys import ApiKeyError

VECTOR = json.loads(
    (Path(__file__).resolve().parents[2] / "proto" / "testdata" / "api-key-hash.json").read_text()
)


def test_hash_matches_the_cross_language_vector() -> None:
    assert apikeys.hash_input(VECTOR["key"]) == VECTOR["hash_input"]
    assert apikeys.hash_api_key(VECTOR["key"]) == VECTOR["blake3"]


def test_secret_keeps_its_underscores_and_the_prefix_is_not_hashed() -> None:
    assert apikeys.hash_input("hull_team1_part_two") == "team1_part_two"
    assert apikeys.hash_api_key("hull_team1_part_two") != apikeys.hash_api_key("hull_team1_part")


@pytest.mark.parametrize(
    "key",
    [
        "sk_abc_def",
        "hull_abc",
        "hull__abc",
        "hull_abc_",
        "hull_a-b_abc",
        "hull_abc_sec ret",
        "hull_été_abc",
        "hull_abc_sécret",
        "HULL_abc_def",
    ],
)
def test_malformed_keys_are_rejected(key: str) -> None:
    with pytest.raises(ApiKeyError, match="hull_<id>_<secret>"):
        apikeys.hash_api_key(key)


def test_env_source_names_the_entry_and_never_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTE_KEYS", "hull_a1_first, ,hull_a2_second")
    assert apikeys.load_api_keys("env:ROUTE_KEYS") == ["hull_a1_first", "hull_a2_second"]
    monkeypatch.setenv("ROUTE_KEYS", "hull_a1_first,leakedsecretvalue")
    with pytest.raises(ApiKeyError) as raised:
        apikeys.load_api_keys("env:ROUTE_KEYS")
    message = str(raised.value)
    assert "entry 2 of env:ROUTE_KEYS" in message
    assert "leakedsecretvalue" not in message and "first" not in message


def test_file_source_names_the_line_and_never_the_key(tmp_path: Path) -> None:
    keys = tmp_path / "keys"
    keys.write_text("hull_a1_first\n\nhull_a2_second\n")
    assert apikeys.load_api_keys(f"file:{keys}") == ["hull_a1_first", "hull_a2_second"]
    keys.write_text("hull_a1_first\n\nhull-a2-leakedsecretvalue\n")
    with pytest.raises(ApiKeyError) as raised:
        apikeys.load_api_keys(f"file:{keys}")
    message = str(raised.value)
    assert f"line 3 of file:{keys}" in message
    assert "leakedsecretvalue" not in message


def test_missing_sources_yield_no_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ROUTE_KEYS", raising=False)
    assert apikeys.load_api_keys("env:ROUTE_KEYS") == []
    assert apikeys.load_api_keys(f"file:{tmp_path / 'missing'}") == []
    with pytest.raises(ApiKeyError, match="unsupported"):
        apikeys.load_api_keys("vault:x")


def test_api_key_hashes_are_sorted_and_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROUTE_KEYS", "hull_b2_two,hull_a1_one,hull_b2_two")
    hashes = apikeys.api_key_hashes("env:ROUTE_KEYS")
    expected = sorted({apikeys.hash_api_key("hull_a1_one"), apikeys.hash_api_key("hull_b2_two")})
    assert hashes == expected
