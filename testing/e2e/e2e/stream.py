from __future__ import annotations

import ipaddress
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

TOKEN_ENV = "MULTIHULL_DISCOVERY_TOKEN"

PrivateKey = ec.EllipticCurvePrivateKey
Issuer = tuple[x509.Certificate, PrivateKey]


@dataclass(frozen=True)
class StreamCredentials:
    ca: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path
    foreign_ca: Path
    token: str = field(repr=False)

    @property
    def env(self) -> dict[str, str]:
        return {TOKEN_ENV: self.token}

    def controller_args(self) -> list[str]:
        return [
            "--tls-cert",
            str(self.server_cert),
            "--tls-key",
            str(self.server_key),
            "--client-ca",
            str(self.ca),
            "--token-env",
            TOKEN_ENV,
        ]

    def router_snapshot(self, address: str) -> dict[str, str]:
        return {
            "source": f"grpcs://{address}",
            "ca": str(self.ca),
            "client_cert": str(self.client_cert),
            "client_key": str(self.client_key),
            "token_env": TOKEN_ENV,
        }


def certificate(
    subject: str,
    public_key: ec.EllipticCurvePublicKey,
    issuer: Issuer | None,
    signing_key: PrivateKey,
) -> x509.CertificateBuilder:
    now = datetime.now(UTC)
    subject_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)])
    return (
        x509.CertificateBuilder()
        .subject_name(subject_name)
        .issuer_name(issuer[0].subject if issuer else subject_name)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signing_key.public_key()),
            critical=False,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
    )


def new_ca(common_name: str) -> Issuer:
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        certificate(common_name, key.public_key(), None, key)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return cert, key


def new_leaf(
    common_name: str, issuer: Issuer, usage: x509.ObjectIdentifier, sans: list[x509.GeneralName]
) -> Issuer:
    key = ec.generate_private_key(ec.SECP256R1())
    builder = (
        certificate(common_name, key.public_key(), issuer, issuer[1])
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    return builder.sign(issuer[1], hashes.SHA256()), key


def write_cert(path: Path, cert: x509.Certificate) -> Path:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


def write_key(path: Path, key: PrivateKey) -> Path:
    path.touch(mode=0o600)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


def generate_credentials(directory: Path) -> StreamCredentials:
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    ca = new_ca("multihull e2e ca")
    server_names: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    server = new_leaf("hull controller", ca, ExtendedKeyUsageOID.SERVER_AUTH, server_names)
    client = new_leaf("multihull router", ca, ExtendedKeyUsageOID.CLIENT_AUTH, [])
    foreign, _ = new_ca("unrelated ca")
    return StreamCredentials(
        ca=write_cert(directory / "ca.crt", ca[0]),
        server_cert=write_cert(directory / "controller.crt", server[0]),
        server_key=write_key(directory / "controller.key", server[1]),
        client_cert=write_cert(directory / "router.crt", client[0]),
        client_key=write_key(directory / "router.key", client[1]),
        foreign_ca=write_cert(directory / "foreign-ca.crt", foreign),
        token=secrets.token_urlsafe(32),
    )
