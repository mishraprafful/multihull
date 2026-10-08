from __future__ import annotations

import hmac
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import grpc

DEFAULT_TOKEN_ENV = "MULTIHULL_DISCOVERY_TOKEN"
AUTHORIZATION_KEY = "authorization"


class StreamSecurityError(ValueError):
    pass


@dataclass(frozen=True)
class StreamSecurity:
    tls_cert: Path | None = None
    tls_key: Path | None = None
    client_ca: Path | None = None
    token: str | None = field(default=None, repr=False)
    token_env: str = DEFAULT_TOKEN_ENV
    insecure: bool = False

    @classmethod
    def from_options(
        cls,
        tls_cert: Path | None,
        tls_key: Path | None,
        client_ca: Path | None,
        token_env: str,
        insecure: bool,
    ) -> StreamSecurity:
        if not token_env.strip():
            raise StreamSecurityError("--token-env names no environment variable")
        token = os.environ.get(token_env, "").strip() or None
        security = cls(tls_cert, tls_key, client_ca, token, token_env, insecure)
        security.validate()
        return security

    @property
    def tls(self) -> bool:
        return self.tls_cert is not None

    @property
    def mutual(self) -> bool:
        return self.client_ca is not None

    def validate(self) -> None:
        tls_files = [self.tls_cert, self.tls_key, self.client_ca]
        if self.insecure and any(path is not None for path in tls_files):
            raise StreamSecurityError(
                "--insecure cannot be combined with --tls-cert, --tls-key or --client-ca"
            )
        if self.insecure:
            return
        if self.tls_cert is None and self.tls_key is None:
            raise StreamSecurityError(
                "refusing to serve the discovery stream in plaintext: snapshots can carry "
                "provider credentials. Pass --tls-cert and --tls-key (plus --client-ca for "
                "mTLS), or --insecure for local development only"
            )
        if self.tls_cert is None or self.tls_key is None:
            raise StreamSecurityError("--tls-cert and --tls-key must be passed together")
        if self.token is None and not self.mutual:
            raise StreamSecurityError(
                f"no client authentication: set {self.token_env} to a bootstrap token, "
                "pass --client-ca for mTLS, or both"
            )

    def server_credentials(self) -> grpc.ServerCredentials:
        if self.tls_cert is None or self.tls_key is None:
            raise StreamSecurityError("TLS needs --tls-cert and --tls-key")
        return grpc.ssl_server_credentials(
            [(read_pem(self.tls_key, "--tls-key"), read_pem(self.tls_cert, "--tls-cert"))],
            root_certificates=read_pem(self.client_ca, "--client-ca") if self.client_ca else None,
            require_client_auth=self.mutual,
        )

    def authorization_problem(
        self, metadata: Sequence[tuple[str, str | bytes]] | None
    ) -> str | None:
        if self.token is None:
            return None
        presented = [value for key, value in metadata or () if key.lower() == AUTHORIZATION_KEY]
        if not presented:
            return "missing bearer token"
        expected = f"Bearer {self.token}".encode()
        value = presented[0]
        actual = value if isinstance(value, bytes) else value.encode()
        if len(presented) > 1 or not hmac.compare_digest(actual.strip(), expected):
            return "invalid bearer token"
        return None

    def describe(self) -> str:
        if self.insecure:
            transport = "plaintext"
        elif self.mutual:
            transport = "TLS with client certificates"
        else:
            transport = "TLS"
        auth = f"bearer token from {self.token_env}" if self.token else "no bearer token"
        return f"{transport}, {auth}"


def read_pem(path: Path, option: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise StreamSecurityError(f"{option} {path}: {exc.strerror or exc}") from None
    if b"-----BEGIN" not in data:
        raise StreamSecurityError(f"{option} {path}: not a PEM file")
    return data
