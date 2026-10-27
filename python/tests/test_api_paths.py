from __future__ import annotations

import pytest

from multihull.providers.base import api_path, mirrored_names


def test_api_path_quotes_every_segment() -> None:
    assert api_path("models", "a/b", "deployments") == "/models/a%2Fb/deployments"
    assert api_path("account", "secrets", "x y") == "/account/secrets/x%20y"


@pytest.mark.parametrize("segment", ["", ".", ".."])
def test_api_path_refuses_empty_and_dot_segments(segment: str) -> None:
    with pytest.raises(ValueError, match="dot segment"):
        api_path("secrets", segment)


def test_mirrored_names_accept_only_the_store_charset() -> None:
    assert mirrored_names("multihull_a_b_c,multihull_d") == ["multihull_a_b_c", "multihull_d"]
    assert mirrored_names("") == []
    for joined in ("../models/m1", "ok,../x", "a b", "a.b"):
        with pytest.raises(ValueError, match="refusing"):
            mirrored_names(joined)
