from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from multihull.build_context import (
    excluded,
    ignore_rules,
    read_rules,
    scan_context,
    shipped_files,
)
from multihull.state import LocalState
from tests.fake_replicate import EXAMPLE


@pytest.fixture
def context(tmp_path: Path) -> Path:
    root = tmp_path / "echo"
    shutil.copytree(EXAMPLE, root)
    return root


@pytest.mark.parametrize(
    ("patterns", "path", "expected"),
    [
        ([".multihull"], ".multihull/state.db", True),
        (["/.multihull/"], ".multihull/plan/a.json", True),
        (["*.db"], "state.db", True),
        (["*.db"], "data/state.db", False),
        (["**/*.db"], "data/deep/state.db", True),
        (["**/__pycache__"], "pkg/__pycache__/x.pyc", True),
        (["data/*"], "data/a/b.txt", True),
        (["weights", "!weights/keep.bin"], "weights/keep.bin", False),
        (["weights", "!weights/keep.bin"], "weights/drop.bin", True),
        (["model?.bin"], "model1.bin", True),
        (["model[0-9].bin"], "modelx.bin", False),
        (["model[!0-9].bin"], "modelx.bin", True),
        (["# comment", "", "./notes.txt"], "notes.txt", True),
    ],
)
def test_dockerignore_patterns_follow_docker(
    patterns: list[str], path: str, expected: bool
) -> None:
    assert excluded(path, ignore_rules(patterns)) is expected


def test_shipped_files_honour_the_example_dockerignore(context: Path) -> None:
    (context / ".multihull").mkdir()
    (context / ".multihull" / "snapshot.json").write_text("{}")
    assert list(shipped_files(context, read_rules(context))) == [
        ".dockerignore",
        "cog.yaml",
        "predict.py",
    ]


def test_the_digest_covers_only_what_docker_copies(context: Path, tmp_path: Path) -> None:
    before = scan_context(context)
    assert before.problems == []
    for unhashed in (".git", ".cog", "__pycache__"):
        (context / unhashed).mkdir()
        (context / unhashed / "noise").write_text("changes on every build")
    (context / "multihull.yaml").write_text("edited spec")
    (context / ".multihull").mkdir()
    (context / ".multihull" / "state.db").write_text("rewritten on every deploy")
    assert scan_context(context).digest == before.digest
    (context / "weights.txt").write_text("new file")
    with_file = scan_context(context).digest
    assert with_file != before.digest
    (context / "weights").symlink_to(tmp_path, target_is_directory=True)
    assert scan_context(context).digest != with_file


def test_local_state_inside_the_context_is_a_problem_unless_ignored(context: Path) -> None:
    (context / ".dockerignore").write_text("README.md\nmultihull.yaml\n")
    (context / ".multihull" / "plan").mkdir(parents=True)
    (context / ".multihull" / "plan" / "replicate.json").write_text("{}")
    (context / ".multihull" / "snapshot.json").write_text("{}")
    assert scan_context(context).problems == [
        "ships .multihull/ (Multihull state, plans and snapshots)"
    ]
    (context / ".dockerignore").write_text(".multihull\n")
    assert scan_context(context).problems == []


def test_a_snapshot_or_state_elsewhere_in_the_context_is_a_problem(context: Path) -> None:
    padding = "x" * (1 << 20)
    document = {"pad": padding[:-7], "routes": [{"endpoints": [{"inject_headers": {}}]}]}
    (context / "router-snapshot.json").write_text(json.dumps(document))
    LocalState(context / "custom.db").list("echo")
    assert scan_context(context).problems == [
        "ships custom.db, a Multihull state database",
        "ships router-snapshot.json, a Multihull snapshot with provider credentials",
    ]


def test_a_credentials_file_inside_the_context_is_a_problem_unless_ignored(
    context: Path, tmp_path: Path
) -> None:
    named = context / "keys" / "replicate.token"
    named.parent.mkdir()
    named.write_text("fixture value")
    outside = tmp_path / "elsewhere.token"
    outside.write_text("fixture value")
    assert scan_context(context, [named, outside]).problems == [
        "ships keys/replicate.token, a credentials file a target names"
    ]
    (context / ".dockerignore").write_text(".multihull\nkeys\n")
    assert scan_context(context, [named, outside]).problems == []


def test_a_credentials_file_is_found_by_identity_not_by_spelling(
    context: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "keys" / "replicate.token"
    outside.parent.mkdir()
    outside.write_text("fixture value")
    os.link(outside, context / "linked.token")
    assert scan_context(context, [outside]).problems == [
        "ships linked.token, a credentials file a target names"
    ]
    (context / "linked.token").unlink()
    (context / "tok").write_text("fixture value")
    (context / "sub").mkdir()
    respelled = context / "sub" / ".." / "tok"
    assert scan_context(context, [respelled]).problems == [
        "ships tok, a credentials file a target names"
    ]
    if (context / "TOK").exists():
        assert scan_context(context, [context / "TOK"]).problems == [
            "ships tok, a credentials file a target names"
        ]
