from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import grpc
import pytest
from typer.testing import CliRunner

from multihull._proto import discovery_pb2 as pb
from multihull._proto import discovery_pb2_grpc as pb_grpc
from multihull.cli import app
from multihull.spec import ServiceSpec
from multihull.stream_security import (
    DEFAULT_TOKEN_ENV,
    StreamSecurity,
    StreamSecurityError,
)
from tests.certs import Pki, generate
from tests.fakes import FakeProvider
from tests.test_controller import make_controller

TOKEN = "bootstrap-token-for-tests"
runner = CliRunner()


@pytest.fixture
def pki(tmp_path: Path) -> Pki:
    return generate(tmp_path / "pki")


def secured(pki: Pki, token: str | None = TOKEN, mutual: bool = True) -> StreamSecurity:
    return StreamSecurity(
        tls_cert=pki.server_cert,
        tls_key=pki.server_key,
        client_ca=pki.ca if mutual else None,
        token=token,
    )


def channel_credentials(
    pki: Pki, with_client_cert: bool = True, ca: Path | None = None
) -> grpc.ChannelCredentials:
    return grpc.ssl_channel_credentials(
        root_certificates=(ca or pki.ca).read_bytes(),
        private_key=pki.client_key.read_bytes() if with_client_cert else None,
        certificate_chain=pki.client_cert.read_bytes() if with_client_cert else None,
    )


def first_reply(
    spec: ServiceSpec,
    tmp_path: Path,
    security: StreamSecurity,
    credentials: grpc.ChannelCredentials | None,
    metadata: tuple[tuple[str, str], ...] = (),
) -> tuple[pb.ControlMessage | None, grpc.StatusCode | None, int]:
    providers = {t.provider: FakeProvider() for t in spec.targets}
    controller, _ = make_controller(spec, tmp_path, providers)

    async def scenario() -> tuple[pb.ControlMessage | None, grpc.StatusCode | None, int]:
        server, port = await controller.serve("127.0.0.1:0", security)
        outgoing: asyncio.Queue[pb.RouterMessage | None] = asyncio.Queue()

        async def requests() -> AsyncIterator[pb.RouterMessage]:
            while (message := await outgoing.get()) is not None:
                yield message

        target = f"127.0.0.1:{port}"
        channel = (
            grpc.aio.insecure_channel(target)
            if credentials is None
            else grpc.aio.secure_channel(target, credentials)
        )
        try:
            async with channel:
                call = pb_grpc.DiscoveryStub(channel).Stream(requests(), metadata=metadata)
                await outgoing.put(pb.RouterMessage(hello=pb.Hello(node_id="router-1")))
                try:
                    reply = await asyncio.wait_for(call.read(), timeout=10)
                except grpc.aio.AioRpcError as exc:
                    return None, exc.code(), len(controller.subscribers)
                subscribers = len(controller.subscribers)
                await outgoing.put(None)
                return reply, None, subscribers
        finally:
            await server.stop(None)

    return asyncio.run(scenario())


def bearer(token: str) -> tuple[tuple[str, str], ...]:
    return (("authorization", f"Bearer {token}"),)


def test_good_token_over_mtls_streams_the_snapshot(
    llama_spec: ServiceSpec, tmp_path: Path, pki: Pki, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="multihull.controller")
    reply, code, subscribers = first_reply(
        llama_spec, tmp_path, secured(pki), channel_credentials(pki), bearer(TOKEN)
    )
    assert code is None
    assert reply is not None and reply.snapshot.version == 1
    assert subscribers == 1
    assert "TLS with client certificates, bearer token from MULTIHULL_DISCOVERY_TOKEN" in (
        caplog.text
    )
    assert TOKEN not in caplog.text


@pytest.mark.parametrize(
    "metadata", [bearer("not-the-token"), (), (("authorization", TOKEN),)], ids=str
)
def test_wrong_or_missing_token_is_unauthenticated(
    llama_spec: ServiceSpec,
    tmp_path: Path,
    pki: Pki,
    caplog: pytest.LogCaptureFixture,
    metadata: tuple[tuple[str, str], ...],
) -> None:
    caplog.set_level(logging.DEBUG, logger="multihull.controller")
    reply, code, subscribers = first_reply(
        llama_spec, tmp_path, secured(pki), channel_credentials(pki), metadata
    )
    assert reply is None
    assert code is grpc.StatusCode.UNAUTHENTICATED
    assert subscribers == 0
    reason = "missing bearer token" if not metadata else "invalid bearer token"
    assert "rejected discovery stream from ipv4:127.0.0.1:" in caplog.text
    assert reason in caplog.text
    assert TOKEN not in caplog.text
    assert "not-the-token" not in caplog.text


def test_token_only_tls_without_client_certificates(
    llama_spec: ServiceSpec, tmp_path: Path, pki: Pki
) -> None:
    security = secured(pki, mutual=False)
    reply, code, _ = first_reply(
        llama_spec,
        tmp_path,
        security,
        channel_credentials(pki, with_client_cert=False),
        bearer(TOKEN),
    )
    assert code is None and reply is not None
    _, code, _ = first_reply(
        llama_spec, tmp_path, security, channel_credentials(pki, with_client_cert=False)
    )
    assert code is grpc.StatusCode.UNAUTHENTICATED


def test_mtls_rejects_a_router_without_a_client_certificate(
    llama_spec: ServiceSpec, tmp_path: Path, pki: Pki
) -> None:
    reply, code, subscribers = first_reply(
        llama_spec,
        tmp_path,
        secured(pki),
        channel_credentials(pki, with_client_cert=False),
        bearer(TOKEN),
    )
    assert reply is None
    assert code is grpc.StatusCode.UNAVAILABLE
    assert subscribers == 0


def test_router_rejects_a_controller_certificate_from_another_ca(
    llama_spec: ServiceSpec, tmp_path: Path, pki: Pki
) -> None:
    reply, code, _ = first_reply(
        llama_spec,
        tmp_path,
        secured(pki),
        channel_credentials(pki, ca=pki.foreign_ca),
        bearer(TOKEN),
    )
    assert reply is None
    assert code is grpc.StatusCode.UNAVAILABLE


def test_mtls_without_a_token_accepts_certificate_holders(
    llama_spec: ServiceSpec, tmp_path: Path, pki: Pki
) -> None:
    reply, code, _ = first_reply(
        llama_spec, tmp_path, secured(pki, token=None), channel_credentials(pki)
    )
    assert code is None and reply is not None


def test_insecure_serves_plaintext_and_warns(
    llama_spec: ServiceSpec, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="multihull.controller")
    reply, code, _ = first_reply(llama_spec, tmp_path, StreamSecurity(insecure=True), None)
    assert code is None and reply is not None
    warning = next(r for r in caplog.records if r.levelno == logging.WARNING)
    assert "plaintext and unauthenticated (--insecure)" in warning.getMessage()

    caplog.clear()
    insecure_with_token = StreamSecurity(insecure=True, token=TOKEN)
    _, code, _ = first_reply(llama_spec, tmp_path, insecure_with_token, None)
    assert code is grpc.StatusCode.UNAUTHENTICATED
    reply, code, _ = first_reply(llama_spec, tmp_path, insecure_with_token, None, bearer(TOKEN))
    assert code is None and reply is not None
    assert TOKEN not in caplog.text


def test_validation_refuses_plaintext_and_unauthenticated_tls(pki: Pki) -> None:
    with pytest.raises(StreamSecurityError, match="refusing to serve"):
        StreamSecurity().validate()
    with pytest.raises(StreamSecurityError, match="cannot be combined"):
        StreamSecurity(insecure=True, client_ca=pki.ca).validate()
    with pytest.raises(StreamSecurityError, match="together"):
        StreamSecurity(tls_cert=pki.server_cert, token=TOKEN).validate()
    with pytest.raises(StreamSecurityError, match="no client authentication"):
        StreamSecurity(tls_cert=pki.server_cert, tls_key=pki.server_key).validate()
    secured(pki).validate()
    secured(pki, token=None).validate()
    secured(pki, mutual=False).validate()
    StreamSecurity(insecure=True).validate()
    assert TOKEN not in repr(secured(pki))


def test_from_options_reads_the_named_env_var(pki: Pki, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CUSTOM_TOKEN", f"  {TOKEN}\n")
    security = StreamSecurity.from_options(
        pki.server_cert, pki.server_key, None, "CUSTOM_TOKEN", False
    )
    assert security.token == TOKEN
    monkeypatch.setenv("CUSTOM_TOKEN", "")
    with pytest.raises(StreamSecurityError, match="CUSTOM_TOKEN"):
        StreamSecurity.from_options(pki.server_cert, pki.server_key, None, "CUSTOM_TOKEN", False)
    with pytest.raises(StreamSecurityError, match="--token-env"):
        StreamSecurity.from_options(pki.server_cert, pki.server_key, None, " ", False)


def test_unreadable_or_non_pem_files_are_reported(pki: Pki, tmp_path: Path) -> None:
    missing = StreamSecurity(tls_cert=tmp_path / "nope.crt", tls_key=pki.server_key, token=TOKEN)
    with pytest.raises(StreamSecurityError, match="--tls-cert .*nope.crt"):
        missing.server_credentials()
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("hello")
    with pytest.raises(StreamSecurityError, match="not a PEM file"):
        StreamSecurity(
            tls_cert=pki.server_cert, tls_key=pki.server_key, client_ca=garbage
        ).server_credentials()


def run_controller(args: list[str], spec: Path) -> tuple[int, str]:
    result = runner.invoke(app, ["controller", str(spec), *args])
    return result.exit_code, " ".join(result.output.split())


def test_controller_command_refuses_to_start_without_tls_or_insecure(
    tmp_path: Path, pki: Pki, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(DEFAULT_TOKEN_ENV, raising=False)
    spec = tmp_path / "multihull.yaml"
    code, output = run_controller([], spec)
    assert code == 2
    assert "refusing to serve the discovery stream in plaintext" in output
    assert "--insecure" in output

    code, output = run_controller(["--insecure", "--tls-cert", str(pki.server_cert)], spec)
    assert code == 2 and "cannot be combined" in output

    tls = ["--tls-cert", str(pki.server_cert), "--tls-key", str(pki.server_key)]
    code, output = run_controller(tls, spec)
    assert code == 2 and f"set {DEFAULT_TOKEN_ENV}" in output

    monkeypatch.setenv(DEFAULT_TOKEN_ENV, TOKEN)
    code, output = run_controller(
        ["--tls-cert", str(tmp_path / "missing.crt"), "--tls-key", str(pki.server_key)], spec
    )
    assert code == 2 and "missing.crt" in output
    assert TOKEN not in output
