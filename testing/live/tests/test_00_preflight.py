from __future__ import annotations

from pathlib import Path

import pytest

from live import preflight
from live.preflight import annotation, render, run
from multihull.providers.modal import CredentialCheck, CredentialStatus

FAKE_ID = "ak-fakeFAKEfake0123456789"
FAKE_SECRET = "as-fakeFAKEfake9876543210"
ACCEPTED = CredentialCheck(
    CredentialStatus.ACCEPTED, "Modal accepted MODAL_TOKEN_ID/MODAL_TOKEN_SECRET"
)
REJECTED = CredentialCheck(
    CredentialStatus.REJECTED,
    "Modal rejected MODAL_TOKEN_ID/MODAL_TOKEN_SECRET: Token validation failed",
)


def env(token_id: str | None = FAKE_ID, secret: str | None = FAKE_SECRET) -> dict[str, str]:
    values = {"MODAL_TOKEN_ID": token_id, "MODAL_TOKEN_SECRET": secret}
    return {name: value for name, value in values.items() if value is not None}


def assert_no_values(text: str) -> None:
    for value in (FAKE_ID, FAKE_SECRET):
        assert value not in text and value[3:] not in text and value[3:12] not in text


def test_correct_shape_accepted() -> None:
    result = run(env(), lambda: ACCEPTED)
    report = render(result, "acme/multihull")
    assert result.ok and result.status == "accepted"
    assert "| `MODAL_TOKEN_ID` | yes | 25 | yes (`ak-`) | none |" in report
    assert "| `MODAL_TOKEN_SECRET` | yes | 25 | yes (`as-`) | none |" in report
    assert "| Looks swapped | no |" in report
    assert "### Fix" not in report and annotation(result) == ""
    assert_no_values(report)


def test_swapped_values_are_flagged() -> None:
    result = run(env(FAKE_SECRET, FAKE_ID), lambda: REJECTED)
    report = render(result, "acme/multihull")
    assert not result.ok and result.swapped
    assert "| Looks swapped | yes |" in report
    assert "no (expected `ak-`)" in report and "no (expected `as-`)" in report
    assert "gh secret set MODAL_TOKEN_ID --repo acme/multihull" in report
    assert_no_values(report + annotation(result))


def test_trailing_newline_is_reported_by_position_and_kind() -> None:
    result = run(env(secret=FAKE_SECRET + "\n"), lambda: REJECTED)
    report = render(result)
    assert "| `MODAL_TOKEN_SECRET` | yes | 26 | yes (`as-`) | trailing newline |" in report
    assert "`MODAL_TOKEN_SECRET` has trailing newline" in report
    assert "**Credentials rejected.**" in report
    assert annotation(result).startswith("::error::Modal credentials rejected:")
    assert_no_values(report + annotation(result))


def test_inner_and_leading_whitespace() -> None:
    result = run(env(token_id=" ak-fake fake"), lambda: REJECTED)
    assert result.shapes[0].whitespace == ("leading space", "inner space")
    assert not result.shapes[0].prefix_ok


def test_missing_variable_skips_the_call() -> None:
    def never() -> CredentialCheck:
        raise AssertionError("must not call Modal")

    result = run(env(secret=None), never)
    report = render(result)
    assert result.status == "missing" and not result.ok
    assert "| `MODAL_TOKEN_SECRET` | no | 0 | n/a | none |" in report
    assert "skipped, a variable is missing" in report and "### Fix" in report


def test_accepted_despite_whitespace_only_warns() -> None:
    result = run(env(secret=FAKE_SECRET + " "), lambda: ACCEPTED)
    assert result.ok
    assert annotation(result).startswith("::warning::")
    assert "### Fix" not in render(result)


def test_unreachable_does_not_ask_for_a_new_token() -> None:
    unreachable = CredentialCheck(CredentialStatus.UNREACHABLE, "no answer from Modal within 10 s")
    result = run(env(), lambda: unreachable)
    report = render(result)
    assert "**Credentials unreachable.**" in report and "status.modal.com" in report
    assert "### Fix" not in report


def test_printed_output_never_carries_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "summary.md"
    output = tmp_path / "output.txt"
    for name, value in env(token_id=FAKE_SECRET + " ", secret=FAKE_ID + "\n").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/multihull")
    monkeypatch.setattr(preflight, "run", lambda env: run(env, lambda: REJECTED))

    assert preflight.main() == 1

    printed = capsys.readouterr().out
    written = summary.read_text() + output.read_text()
    assert "::error::Modal credentials rejected:" in printed
    assert "gh secret set MODAL_TOKEN_ID --repo acme/multihull" in written
    assert "credentials=rejected" in written
    assert_no_values(printed + written)
