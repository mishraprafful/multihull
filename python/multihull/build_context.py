from __future__ import annotations

import hashlib
import os
import posixpath
import re
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

DOCKERIGNORE = ".dockerignore"
UNHASHED_DIRS = frozenset({".git", ".cog", "__pycache__"})
LOCAL_STATE_DIR = ".multihull"
SQLITE_MAGIC = b"SQLite format 3\x00"
STATE_TABLE = "snapshot_versions"
SNAPSHOT_MARKER = b'"inject_headers"'
READ_CHUNK = 1 << 20


@dataclass(frozen=True)
class IgnoreRule:
    pattern: re.Pattern[str]
    negated: bool


@dataclass
class ContextScan:
    digest: str
    problems: list[str] = field(default_factory=list)


def pattern_regex(pattern: str) -> re.Pattern[str]:
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            parts.append(".*")
            index += 2
            continue
        if char == "*":
            parts.append("[^/]*")
        elif char == "?":
            parts.append("[^/]")
        elif char == "\\" and index + 1 < len(pattern):
            index += 1
            parts.append(re.escape(pattern[index]))
        elif char == "[":
            end = pattern.find("]", index + 1)
            if end == -1:
                parts.append(re.escape(char))
            else:
                body = pattern[index + 1 : end]
                body = "^" + body[1:] if body.startswith(("!", "^")) else body
                parts.append(f"[{body}]")
                index = end
        else:
            parts.append(re.escape(char))
        index += 1
    return re.compile("".join(parts))


def ignore_rules(lines: Sequence[str]) -> list[IgnoreRule]:
    rules: list[IgnoreRule] = []
    for line in lines:
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        negated = text.startswith("!")
        text = text[1:].strip() if negated else text
        cleaned = posixpath.normpath(text.lstrip("/")) if text.lstrip("/") else "."
        if cleaned == ".":
            continue
        rules.append(IgnoreRule(pattern_regex(cleaned), negated))
    return rules


def read_rules(root: Path) -> list[IgnoreRule]:
    path = root / DOCKERIGNORE
    if not path.is_file():
        return []
    return ignore_rules(path.read_text(errors="replace").splitlines())


def lineage(relative: str) -> Iterator[str]:
    pieces = relative.split("/")
    for end in range(1, len(pieces) + 1):
        yield "/".join(pieces[:end])


def excluded(relative: str, rules: Sequence[IgnoreRule]) -> bool:
    matched = False
    for rule in rules:
        if any(rule.pattern.fullmatch(path) for path in lineage(relative)):
            matched = not rule.negated
    return matched


def shipped_files(
    root: Path, rules: Sequence[IgnoreRule], skip: frozenset[str] = frozenset()
) -> Iterator[str]:
    prunable = not any(rule.negated for rule in rules)
    for directory, subdirs, files in os.walk(root):
        base = Path(directory).relative_to(root).as_posix()
        prefix = "" if base == "." else f"{base}/"
        linked = [d for d in subdirs if (Path(directory) / d).is_symlink()]
        subdirs[:] = sorted(
            d
            for d in subdirs
            if d not in linked
            and d not in skip
            and not (prunable and excluded(f"{prefix}{d}", rules))
        )
        for name in sorted([*files, *linked]):
            relative = f"{prefix}{name}"
            if not excluded(relative, rules):
                yield relative


def multihull_state(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            if handle.read(len(SQLITE_MAGIC)) != SQLITE_MAGIC:
                return False
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            found = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (STATE_TABLE,)
            ).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return False
    return found is not None


def hash_file(path: Path, digest: hashlib._Hash) -> bool:
    tail = b""
    marked = False
    with path.open("rb") as handle:
        while chunk := handle.read(READ_CHUNK):
            digest.update(chunk)
            marked = marked or SNAPSHOT_MARKER in tail + chunk
            tail = chunk[-(len(SNAPSHOT_MARKER) - 1) :]
    return marked


def inside(root: Path, paths: Sequence[Path]) -> set[str]:
    base = root.resolve()
    resolved = (path.resolve() for path in paths)
    return {path.relative_to(base).as_posix() for path in resolved if path.is_relative_to(base)}


def scan_context(root: Path, protected: Sequence[Path] = ()) -> ContextScan:
    digest = hashlib.sha256()
    problems: list[str] = []
    rules = read_rules(root)
    guarded = inside(root, protected)
    for relative in shipped_files(root, rules, UNHASHED_DIRS):
        if relative in guarded:
            problems.append(f"ships {relative}, a credentials file a target names")
            continue
        pieces = relative.split("/")
        if LOCAL_STATE_DIR in pieces[:-1]:
            local = "/".join(pieces[: pieces.index(LOCAL_STATE_DIR) + 1])
            problem = f"ships {local}/ (Multihull state, plans and snapshots)"
            if problem not in problems:
                problems.append(problem)
            continue
        path = root / relative
        if path.is_symlink():
            digest.update(f"L {relative} {os.readlink(path)}\n".encode())
            continue
        digest.update(f"F {relative}\n".encode())
        if hash_file(path, digest):
            problems.append(f"ships {relative}, a Multihull snapshot with provider credentials")
        elif multihull_state(path):
            problems.append(f"ships {relative}, a Multihull state database")
    return ContextScan(digest.hexdigest()[:16], problems)
