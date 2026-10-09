from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from multihull.providers.modal import (
    TOKEN_ID_ENV,
    TOKEN_SECRET_ENV,
    CredentialCheck,
    CredentialStatus,
    check_credentials,
)

EXPECTED_PREFIX = {TOKEN_ID_ENV: "ak-", TOKEN_SECRET_ENV: "as-"}
TOKENS_URL = "https://modal.com/settings/tokens"
STATUS_URL = "https://status.modal.com"
WHITESPACE_NAMES = {"\n": "newline", "\r": "carriage return", "\t": "tab"}


@dataclass(frozen=True)
class TokenShape:
    name: str
    present: bool
    length: int
    prefix_ok: bool
    whitespace: tuple[str, ...]

    @property
    def expected_prefix(self) -> str:
        return EXPECTED_PREFIX[self.name]


def whitespace_kind(char: str) -> str:
    return WHITESPACE_NAMES.get(char, "space")


def whitespace_findings(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    findings: list[str] = []
    if value[0].isspace():
        findings.append(f"leading {whitespace_kind(value[0])}")
    if value[-1].isspace():
        findings.append(f"trailing {whitespace_kind(value[-1])}")
    inner = sorted({whitespace_kind(c) for c in value.strip() if c.isspace()})
    findings.extend(f"inner {kind}" for kind in inner)
    return tuple(findings)


def inspect(name: str, value: str | None) -> TokenShape:
    text = value or ""
    return TokenShape(
        name=name,
        present=bool(text),
        length=len(text),
        prefix_ok=text.startswith(EXPECTED_PREFIX[name]),
        whitespace=whitespace_findings(text),
    )


def looks_swapped(env: Mapping[str, str]) -> bool:
    token_id = env.get(TOKEN_ID_ENV, "").strip()
    token_secret = env.get(TOKEN_SECRET_ENV, "").strip()
    return token_id.startswith(EXPECTED_PREFIX[TOKEN_SECRET_ENV]) and token_secret.startswith(
        EXPECTED_PREFIX[TOKEN_ID_ENV]
    )


@dataclass(frozen=True)
class Preflight:
    shapes: tuple[TokenShape, TokenShape]
    swapped: bool
    check: CredentialCheck | None

    @property
    def status(self) -> str:
        return self.check.status.value if self.check else CredentialStatus.MISSING.value

    @property
    def ok(self) -> bool:
        return self.check is not None and self.check.ok

    @property
    def shape_problems(self) -> list[str]:
        problems: list[str] = []
        for shape in self.shapes:
            if not shape.present:
                problems.append(f"`{shape.name}` is missing")
                continue
            if not shape.prefix_ok:
                problems.append(f"`{shape.name}` does not start with `{shape.expected_prefix}`")
            if shape.whitespace:
                problems.append(f"`{shape.name}` has {', '.join(shape.whitespace)}")
        if self.swapped:
            problems.append(f"`{TOKEN_ID_ENV}` and `{TOKEN_SECRET_ENV}` look swapped")
        return problems


def run(
    env: Mapping[str, str], verify: Callable[[], CredentialCheck] = check_credentials
) -> Preflight:
    shapes = (
        inspect(TOKEN_ID_ENV, env.get(TOKEN_ID_ENV)),
        inspect(TOKEN_SECRET_ENV, env.get(TOKEN_SECRET_ENV)),
    )
    check = verify() if all(shape.present for shape in shapes) else None
    return Preflight(shapes=shapes, swapped=looks_swapped(env), check=check)


def yes_no(value: bool) -> str:
    return "yes" if value else "no"


def one_line(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def prefix_cell(shape: TokenShape) -> str:
    if not shape.present:
        return "n/a"
    if shape.prefix_ok:
        return f"yes (`{shape.expected_prefix}`)"
    return f"no (expected `{shape.expected_prefix}`)"


def call_cell(result: Preflight) -> str:
    if result.check is None:
        return "skipped, a variable is missing"
    return one_line(f"{result.check.status.value}: {result.check.message}")


def secret_commands(result: Preflight, repository: str | None = None) -> list[str]:
    repo = f" --repo {repository}" if repository else ""
    return [f"gh secret set {shape.name}{repo}" for shape in result.shapes]


def token_steps(result: Preflight, repository: str | None) -> list[str]:
    return [
        f"1. In the Modal dashboard ({TOKENS_URL}), create a new API token in the workspace "
        "that owns the `main` environment. Copy the token ID (`ak-`) and secret (`as-`) "
        "without surrounding spaces or newlines.",
        "2. Store both, entering each value at the prompt so it stays out of shell history:",
        "",
        "```sh",
        *secret_commands(result, repository),
        "```",
        "",
        "3. Revoke the old token in the dashboard, then re-run this workflow.",
    ]


def verdict(result: Preflight) -> str:
    problems = result.shape_problems
    if problems:
        return "; ".join(problems) + "."
    if result.status in (CredentialStatus.REJECTED, CredentialStatus.INVALID):
        return (
            "The token shape looks right, so check that the token is active and belongs to "
            "the workspace this workflow deploys to."
        )
    if result.status == CredentialStatus.SDK_MISSING:
        return "The modal SDK is missing; run `uv sync --locked --project testing/live`."
    return f"Modal could not be reached; re-run the job and check {STATUS_URL} if it persists."


def needs_new_token(result: Preflight) -> bool:
    return bool(result.shape_problems) or result.status in (
        CredentialStatus.REJECTED,
        CredentialStatus.INVALID,
        CredentialStatus.MISSING,
    )


def render(result: Preflight, repository: str | None = None) -> str:
    lines = [
        "## Modal credential preflight",
        "",
        "| Variable | Present | Length | Expected prefix | Whitespace or newline |",
        "|---|---|---|---|---|",
    ]
    for shape in result.shapes:
        whitespace = ", ".join(shape.whitespace) or "none"
        lines.append(
            f"| `{shape.name}` | {yes_no(shape.present)} | {shape.length} | "
            f"{prefix_cell(shape)} | {whitespace} |"
        )
    lines.extend(
        [
            "",
            "| Check | Result |",
            "|---|---|",
            f"| Looks swapped | {yes_no(result.swapped)} |",
            f"| Authenticated call (same check as `hull doctor`) | {call_cell(result)} |",
            "",
        ]
    )
    if result.ok:
        if result.shape_problems:
            lines.extend([f"Modal accepted the token despite: {verdict(result)}", ""])
        return "\n".join(lines)
    lines.extend(
        [
            f"**Credentials {result.status}.** {verdict(result)} Nothing was built or deployed.",
            "",
        ]
    )
    if needs_new_token(result):
        lines.extend(["### Fix", "", *token_steps(result, repository), ""])
    return "\n".join(lines)


def annotation(result: Preflight) -> str:
    if result.ok:
        if not result.shape_problems:
            return ""
        return f"::warning::Modal accepted the token despite: {verdict(result)}"
    detail = one_line(result.check.message) if result.check else "a variable is missing"
    message = f"::error::Modal credentials {result.status}: {detail}. {verdict(result)}"
    if needs_new_token(result):
        message += (
            f" Create a new token at {TOKENS_URL}, then run "
            f"{' and '.join(secret_commands(result))}, entering each value at the prompt."
        )
    return message


def append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a") as handle:
            handle.write(text)


def main() -> int:
    result = run(os.environ)
    report = render(result, os.environ.get("GITHUB_REPOSITORY"))
    print(report)
    note = annotation(result)
    if note:
        print(note)
    append(os.environ.get("GITHUB_STEP_SUMMARY"), report + "\n")
    append(os.environ.get("GITHUB_OUTPUT"), f"credentials={result.status}\n")
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
