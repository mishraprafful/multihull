from __future__ import annotations

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CREDENTIALS_PATTERN = r"^(env:[A-Za-z_][A-Za-z0-9_]*|file:\S.*)$"
CREDENTIALS_FORMAT = "env:NAME (an environment variable name) or file:PATH"
SMALL_FILE_BYTES = 1 << 20


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class CredentialSource:
    kind: Literal["env", "file"]
    location: str

    @classmethod
    def parse(cls, reference: str) -> CredentialSource:
        if re.fullmatch(CREDENTIALS_PATTERN, reference) is None:
            raise ValueError(f"credentials must be {CREDENTIALS_FORMAT}")
        kind, _, location = reference.partition(":")
        return cls("env" if kind == "env" else "file", location)

    @property
    def path(self) -> Path:
        return Path(self.location).expanduser().absolute()

    def __str__(self) -> str:
        return f"env:{self.location}" if self.kind == "env" else f"file:{self.path}"

    def read(self) -> str:
        value = self.read_env() if self.kind == "env" else self.read_file()
        if not value.strip():
            raise CredentialError(f"credentials {self} is empty")
        return value

    def read_env(self) -> str:
        if self.location not in os.environ:
            raise CredentialError(f"credentials {self} is not set")
        return os.environ[self.location]

    def read_file(self) -> str:
        try:
            return self.path.read_text()
        except UnicodeDecodeError:
            raise CredentialError(f"credentials {self} is not UTF-8 text") from None
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise CredentialError(f"credentials {self} is unreadable: {reason}") from None


def read_credential(reference: str) -> str:
    return CredentialSource.parse(reference).read()


def read_token(reference: str | None, native_env: str) -> str:
    if reference is not None:
        return read_credential(reference).strip()
    value = os.environ.get(native_env, "").strip()
    if not value:
        raise CredentialError(f"{native_env} is not set")
    return value


def token_origin(reference: str | None, native_env: str) -> str:
    return native_env if reference is None else str(CredentialSource.parse(reference))


def credential_problem(reference: str) -> str | None:
    try:
        read_credential(reference)
    except CredentialError as exc:
        return str(exc)
    return None


def read_small_text(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > SMALL_FILE_BYTES:
            return None
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def base64_text(value: object) -> str | None:
    try:
        return base64.b64decode(str(value), validate=True).decode()
    except ValueError:
        return None
