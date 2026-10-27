from __future__ import annotations

import pytest

from multihull.providers import PROVIDERS, create
from multihull.providers.base import Provider


def test_registry_covers_all_types() -> None:
    assert set(PROVIDERS) == {"kubernetes", "modal", "runpod", "baseten", "replicate", "docker"}
    for provider_type in PROVIDERS:
        assert isinstance(create(provider_type), Provider)
    with pytest.raises(ValueError, match="unknown provider type"):
        create("fly")
