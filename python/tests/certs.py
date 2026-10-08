from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

PrivateKey = ec.EllipticCurvePrivateKey


@dataclass(frozen=True)
class Pki:
    ca: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path
    foreign_ca: Path


def name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def builder(
    subject: str, issuer: str, public_key: ec.EllipticCurvePublicKey
) -> x509.CertificateBuilder:
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(name(subject))
        .issuer_name(name(issuer))
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
    )


def new_ca(common_name: str) -> tuple[x509.Certificate, PrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        builder(common_name, common_name, key.public_key())
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
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
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key


def new_leaf(
    common_name: str,
    ca: tuple[x509.Certificate, PrivateKey],
    usage: x509.ObjectIdentifier,
    sans: list[x509.GeneralName],
) -> tuple[x509.Certificate, PrivateKey]:
    ca_cert, ca_key = ca
    key = ec.generate_private_key(ec.SECP256R1())
    issuer = ca_cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
    request = (
        builder(common_name, str(issuer), key.public_key())
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
    )
    if sans:
        request = request.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    return request.sign(ca_key, hashes.SHA256()), key


def write_cert(path: Path, cert: x509.Certificate) -> Path:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


def write_key(path: Path, key: PrivateKey) -> Path:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)
    return path


def generate(directory: Path) -> Pki:
    directory.mkdir(parents=True, exist_ok=True)
    ca = new_ca("multihull test ca")
    server_sans: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    server = new_leaf("controller", ca, ExtendedKeyUsageOID.SERVER_AUTH, server_sans)
    client = new_leaf("router", ca, ExtendedKeyUsageOID.CLIENT_AUTH, [])
    foreign, _ = new_ca("someone else")
    return Pki(
        ca=write_cert(directory / "ca.crt", ca[0]),
        server_cert=write_cert(directory / "server.crt", server[0]),
        server_key=write_key(directory / "server.key", server[1]),
        client_cert=write_cert(directory / "client.crt", client[0]),
        client_key=write_key(directory / "client.key", client[1]),
        foreign_ca=write_cert(directory / "foreign-ca.crt", foreign),
    )
