from __future__ import annotations

import asyncio
import json
import logging
import signal
from datetime import timedelta
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from multihull import deploy as deploymod
from multihull import discovery, engine
from multihull import logs as logsmod
from multihull import spec as specmod
from multihull import top as topmod
from multihull.apikeys import ApiKeyError
from multihull.controller import (
    DEFAULT_DEGRADED_COOLDOWN,
    DEFAULT_GRPC_LISTEN,
    DEFAULT_INTERVAL,
    Controller,
)
from multihull.durations import format_duration, parse_duration
from multihull.providers.base import CredHealth, Provider, Ref
from multihull.state import (
    DEFAULT_STATE,
    STATE_ENV,
    StateBackend,
    StateBackendError,
    open_state,
)
from multihull.stream_security import DEFAULT_TOKEN_ENV, StreamSecurity, StreamSecurityError

app = typer.Typer(
    name="hull",
    help="Deploy always-warm GPU inference containers to many providers from one spec.",
    no_args_is_help=True,
)
console = Console()
errors = Console(stderr=True)

SpecArg = Annotated[Path, typer.Argument(help="Path to multihull.yaml")]
DEFAULT_SPEC = Path("multihull.yaml")
DEFAULT_IMAGE = "ghcr.io/ORG/IMAGE:TAG"
StateOpt = Annotated[
    str,
    typer.Option(
        "--state",
        envvar=STATE_ENV,
        help="State backend: a SQLite file path or sqlite:///absolute/path.db",
    ),
]
SnapshotOutOpt = Annotated[Path, typer.Option("--snapshot-out", help="Snapshot file to write")]
TargetOpt = Annotated[
    list[str] | None, typer.Option("--target", help="Limit to this target (repeatable)")
]


def load_or_exit(path: Path) -> specmod.ServiceSpec:
    try:
        return specmod.load(path)
    except FileNotFoundError:
        errors.print(f"[red]{path} not found[/red]")
        raise typer.Exit(2) from None
    except (ValidationError, ValueError) as exc:
        errors.print(f"[red]{path} is invalid[/red]\n{exc}")
        raise typer.Exit(1) from None


def load_and_warn(path: Path) -> specmod.ServiceSpec:
    service = load_or_exit(path)
    for warning in service.capacity_warnings():
        errors.print(f"[yellow]warning[/yellow] {warning}")
    return service


def route_keys_or_exit(service: specmod.ServiceSpec) -> None:
    try:
        discovery.auth_block(service)
    except ApiKeyError as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None


def provider_for(target: specmod.TargetSpec, live: bool = False) -> Provider:
    return engine.provider_for(target, live)


def providers_for(service: specmod.ServiceSpec, live: bool) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for target in service.targets:
        try:
            providers[target.provider] = provider_for(target, live)
        except Exception as exc:
            errors.print(f"[red]{target.provider}: cannot connect to {target.type}: {exc}[/red]")
            raise typer.Exit(1) from None
    return providers


def reachable_providers(service: specmod.ServiceSpec) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    for target in service.targets:
        try:
            providers[target.provider] = provider_for(target, live=True)
        except Exception as exc:
            errors.print(f"[yellow]{target.provider}: skipping rediscover ({exc})[/yellow]")
    return providers


def rebuild_state(service: specmod.ServiceSpec, state: StateBackend) -> list[str]:
    providers = reachable_providers(service)
    found = [
        result.provider
        for result in engine.rediscover(service, state, providers)
        if result.ref is not None
    ]
    if found:
        engine.refresh(service.name, state, providers)
    return found


def state_or_exit(url: str) -> StateBackend:
    try:
        return open_state(url)
    except StateBackendError as exc:
        errors.print(f"[red]--state ({STATE_ENV}): {exc}[/red]")
        raise typer.Exit(2) from None


def duration_or_exit(text: str, option: str) -> timedelta:
    try:
        return parse_duration(text)
    except ValueError as exc:
        errors.print(f"[red]{option}: {exc}[/red]")
        raise typer.Exit(2) from None


def unknown_targets(service: specmod.ServiceSpec, targets: list[str] | None) -> list[str]:
    known = {t.provider for t in service.targets}
    return [t for t in targets or [] if t not in known]


def default_spec(name: str, has_dockerfile: bool) -> dict:
    container: dict = {
        "command": ["python", "-m", "server"],
        "port": 8000,
        "health": {"path": "/health", "initialDelaySeconds": 60},
    }
    if has_dockerfile:
        container["build"] = {"context": ".", "target": "docker"}
    else:
        container["image"] = DEFAULT_IMAGE
    return {
        "apiVersion": "multihull/v1",
        "name": name,
        "container": container,
        "resources": {"gpu": ["L4", "A10G"], "gpuCount": 1, "memory": "16Gi"},
        "scaling": {"concurrency": 16, "replicas": {"min": 1, "max": 4}},
        "reliability": {
            "minWarmProviders": 2,
            "overprovision": 1.4,
            "fallbackScaleToZero": False,
            "spot": False,
        },
        "targets": [
            {
                "provider": "k8s-primary",
                "type": "kubernetes",
                "priority": 1,
                "kubernetes": {"namespace": "inference"},
            },
            {
                "provider": "modal-warm",
                "type": "modal",
                "priority": 2,
                "modal": {"environment": "main"},
            },
        ],
        "route": {
            "hostname": f"{name}.example.com",
            "protocol": "openai",
            "failover": {
                "policy": "priority",
                "retryOn": ["5xx", "timeout", "capacity"],
                "maxRetries": 2,
            },
            "auth": {"apiKeys": {"from": f"env:{name.upper().replace('-', '_')}_API_KEYS"}},
        },
    }


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Project directory")] = Path("."),
    name: Annotated[str | None, typer.Option(help="Service name")] = None,
    force: Annotated[bool, typer.Option(help="Overwrite an existing multihull.yaml")] = False,
) -> None:
    directory = directory.resolve()
    target = directory / "multihull.yaml"
    if target.exists() and not force:
        errors.print(f"[red]{target} exists; use --force to overwrite[/red]")
        raise typer.Exit(1)
    service_name = name or directory.name.lower().replace("_", "-")
    has_dockerfile = (directory / "Dockerfile").exists()
    document = default_spec(service_name, has_dockerfile)
    specmod.ServiceSpec.model_validate(document)
    target.write_text(yaml.safe_dump(document, sort_keys=False))
    source = "Dockerfile build" if has_dockerfile else f"image {DEFAULT_IMAGE}"
    console.print(f"wrote {target} ({source})")


@app.command()
def validate(path: SpecArg = DEFAULT_SPEC) -> None:
    service = load_and_warn(path)
    console.print(f"[green]ok[/green] {service.name}: {len(service.targets)} targets")


@app.command()
def schema(
    out: Annotated[Path | None, typer.Option(help="Write JSON Schema to this file")] = None,
) -> None:
    document = json.dumps(specmod.json_schema(), indent=2) + "\n"
    if out is None:
        typer.echo(document, nl=False)
        return
    out.write_text(document)
    console.print(f"wrote {out}")


@app.command()
def plan(
    path: SpecArg = DEFAULT_SPEC,
    out: Annotated[
        Path, typer.Option(help="Directory for rendered payloads")
    ] = engine.DEFAULT_PLAN_DIR,
    state_url: StateOpt = DEFAULT_STATE,
) -> None:
    service = load_and_warn(path)
    state = state_or_exit(state_url)
    plans = engine.plan(service, state, factory=provider_for)
    written = engine.write_plan_dir(plans, out)
    table = Table(title=f"plan {service.name}")
    table.add_column("provider")
    table.add_column("type")
    table.add_column("change")
    table.add_column("file")
    files = {p.stem: p for p in written}
    for target_plan in plans:
        file = files.get(target_plan.provider)
        table.add_row(
            target_plan.provider,
            target_plan.type,
            colour_change(target_plan.change),
            str(file) if file else "",
        )
    console.print(table)
    for target_plan in plans:
        for note in target_plan.plan.notes if target_plan.plan else []:
            console.print(f"[yellow]note[/yellow] {target_plan.provider}: {note}")


def colour_change(change: str) -> str:
    colours = {
        "new": "green",
        "changed": "yellow",
        "unchanged": "dim",
        "orphaned": "red",
        "skipped": "dim",
    }
    return f"[{colours[change]}]{change}[/{colours[change]}]"


@app.command()
def status(
    path: SpecArg = DEFAULT_SPEC,
    state_url: StateOpt = DEFAULT_STATE,
) -> None:
    service = load_or_exit(path)
    state = state_or_exit(state_url)
    records = state.list(service.name)
    if not records:
        found = rebuild_state(service, state)
        if found:
            console.print(f"rebuilt state from rediscover: {', '.join(found)}")
            records = state.list(service.name)
    table = Table(title=f"status {service.name}")
    table.add_column("provider")
    table.add_column("type")
    table.add_column("status")
    table.add_column("ref")
    table.add_column("updated")
    if not records:
        console.print(f"no state for {service.name}; run hull deploy first")
        return
    for record in records:
        ref = Ref.from_json(record.ref)
        table.add_row(
            record.provider,
            ref.type,
            record.last_status,
            ", ".join(f"{k}={v}" for k, v in sorted(ref.ids.items())),
            record.updated_at.isoformat(),
        )
    console.print(table)


@app.command()
def deploy(
    path: SpecArg = DEFAULT_SPEC,
    apply: Annotated[
        bool, typer.Option("--apply/--dry-run", help="Perform real provider calls")
    ] = False,
    target: TargetOpt = None,
    wait: Annotated[bool, typer.Option(help="Wait for every target to report Ready")] = True,
    timeout: Annotated[
        str | None, typer.Option(help="Readiness timeout per target, e.g. 15m")
    ] = None,
    snapshot_out: SnapshotOutOpt = deploymod.DEFAULT_SNAPSHOT_PATH,
    state_url: StateOpt = DEFAULT_STATE,
    image_digest: Annotated[str | None, typer.Option(help="Pin the image to this digest")] = None,
) -> None:
    service = load_and_warn(path)
    route_keys_or_exit(service)
    unknown = unknown_targets(service, target)
    if unknown:
        errors.print(f"[red]unknown targets: {', '.join(unknown)}[/red]")
        raise typer.Exit(2)
    ready_timeout = duration_or_exit(timeout, "--timeout") if timeout else None
    state = state_or_exit(state_url)
    report = deploymod.deploy(
        service,
        state,
        providers_for(service, live=apply),
        dry_run=not apply,
        only=set(target) if target else None,
        wait=wait,
        timeout=ready_timeout,
        snapshot_out=snapshot_out,
        image_digest=image_digest,
    )
    mode = "dry run" if report.dry_run else "apply"
    table = Table(title=f"deploy {service.name} ({mode})")
    table.add_column("provider")
    table.add_column("type")
    table.add_column("change")
    table.add_column("result")
    table.add_column("replicas")
    table.add_column("url")
    table.add_column("message")
    for outcome in report.outcomes:
        table.add_row(
            outcome.provider,
            outcome.type,
            colour_change(outcome.change),
            colour_result(outcome.ok, "planned" if report.dry_run else outcome.phase),
            "" if report.dry_run else f"{outcome.ready_replicas}/{outcome.desired_replicas}",
            outcome.url or "",
            outcome.message,
        )
    console.print(table)
    if report.snapshot_path is not None:
        console.print(f"wrote {report.snapshot_path}")
    if report.dry_run:
        console.print("dry run; pass --apply to deploy")
    if not report.ok:
        failed = ", ".join(o.provider for o in report.failed)
        errors.print(f"[red]{len(report.failed)} target(s) failed: {failed}[/red]")
        raise typer.Exit(1)


def colour_result(ok: bool, phase: str) -> str:
    colour = "green" if ok else "red"
    return f"[{colour}]{phase}[/{colour}]"


@app.command()
def destroy(
    path: SpecArg = DEFAULT_SPEC,
    target: TargetOpt = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmation")] = False,
    snapshot_out: SnapshotOutOpt = deploymod.DEFAULT_SNAPSHOT_PATH,
    state_url: StateOpt = DEFAULT_STATE,
) -> None:
    service = load_or_exit(path)
    route_keys_or_exit(service)
    state = state_or_exit(state_url)
    records = [r for r in state.list(service.name) if not target or r.provider in target]
    if not records:
        console.print(f"nothing to destroy for {service.name}")
        return
    names = ", ".join(r.provider for r in records)
    if not yes and not typer.confirm(f"destroy {service.name} on {names}?"):
        raise typer.Exit(1)
    providers = providers_for(service, live=True)
    results = engine.destroy(service.name, state, providers, only=set(target) if target else None)
    table = Table(title=f"destroy {service.name}")
    table.add_column("provider")
    table.add_column("type")
    table.add_column("result")
    table.add_column("message")
    for result in results:
        table.add_row(
            result.provider,
            result.type,
            colour_result(result.ok, "destroyed" if result.ok else "failed"),
            result.message,
        )
    console.print(table)
    document = discovery.snapshot_after_destroy(service, state, providers, snapshot_out)
    console.print(f"wrote {discovery.write_snapshot(document, snapshot_out)}")
    if not all(r.ok for r in results):
        raise typer.Exit(1)


@app.command()
def logs(
    path: SpecArg = DEFAULT_SPEC,
    provider: Annotated[str, typer.Option("--provider", "-p", help="Target provider name")] = "",
    since: Annotated[str, typer.Option(help="How far back to read, e.g. 10m")] = "10m",
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Keep streaming")] = False,
    state_url: StateOpt = DEFAULT_STATE,
) -> None:
    service = load_or_exit(path)
    if not provider:
        errors.print("[red]--provider is required[/red]")
        raise typer.Exit(2)
    if unknown_targets(service, [provider]):
        errors.print(f"[red]unknown target {provider}[/red]")
        raise typer.Exit(2)
    record = state_or_exit(state_url).get(service.name, provider)
    if record is None:
        errors.print(f"[red]no state for {service.name}/{provider}; run hull deploy first[/red]")
        raise typer.Exit(1)
    window = duration_or_exit(since, "--since")
    source = provider_for(service.target(provider), live=True)
    try:
        for line in logsmod.stream_logs(source, Ref.from_json(record.ref), window, follow):
            typer.echo(line)
    except KeyboardInterrupt:
        return
    except RuntimeError as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None


@app.command()
def controller(
    path: SpecArg = DEFAULT_SPEC,
    interval: Annotated[str, typer.Option(help="Reconcile interval, e.g. 30s")] = format_duration(
        DEFAULT_INTERVAL
    ),
    grpc_listen: Annotated[
        str, typer.Option("--grpc-listen", help="host:port for the discovery stream")
    ] = DEFAULT_GRPC_LISTEN,
    snapshot_out: SnapshotOutOpt = deploymod.DEFAULT_SNAPSHOT_PATH,
    state_url: StateOpt = DEFAULT_STATE,
    degraded_cooldown: Annotated[
        str,
        typer.Option(
            "--degraded-cooldown",
            help="Quiet time after the last Degraded signal before one scale-back step, e.g. 10m",
        ),
    ] = format_duration(DEFAULT_DEGRADED_COOLDOWN),
    log_level: Annotated[str, typer.Option(help="Python log level")] = "INFO",
    tls_cert: Annotated[
        Path | None,
        typer.Option("--tls-cert", help="PEM certificate chain the discovery stream serves"),
    ] = None,
    tls_key: Annotated[
        Path | None, typer.Option("--tls-key", help="PEM private key for --tls-cert")
    ] = None,
    client_ca: Annotated[
        Path | None,
        typer.Option(
            "--client-ca",
            help="PEM CA bundle; routers must present a client certificate it signed (mTLS)",
        ),
    ] = None,
    token_env: Annotated[
        str,
        typer.Option(
            "--token-env",
            help="Env var holding the bootstrap token routers send as 'authorization: Bearer'",
        ),
    ] = DEFAULT_TOKEN_ENV,
    insecure: Annotated[
        bool,
        typer.Option(
            "--insecure",
            help="Serve the discovery stream in plaintext; local development only",
        ),
    ] = False,
) -> None:
    logging.basicConfig(
        level=log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    try:
        security = StreamSecurity.from_options(tls_cert, tls_key, client_ca, token_env, insecure)
        if security.tls:
            security.server_credentials()
    except StreamSecurityError as exc:
        errors.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from None
    service = load_or_exit(path)
    route_keys_or_exit(service)
    daemon = Controller(
        service,
        state_or_exit(state_url),
        providers_for(service, live=True),
        snapshot_out=snapshot_out,
        interval=duration_or_exit(interval, "--interval"),
        degraded_cooldown=duration_or_exit(degraded_cooldown, "--degraded-cooldown"),
    )
    try:
        asyncio.run(run_until_terminated(daemon, security, grpc_listen))
    except (KeyboardInterrupt, asyncio.CancelledError):
        return


async def run_until_terminated(daemon: Controller, security: StreamSecurity, listen: str) -> None:
    task = asyncio.current_task()
    if task is not None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    await daemon.run(security, listen)


@app.command()
def snapshot(
    path: SpecArg = DEFAULT_SPEC,
    out: Annotated[Path, typer.Option(help="Snapshot file")] = Path("snapshot.json"),
    state_url: StateOpt = DEFAULT_STATE,
) -> None:
    service = load_or_exit(path)
    route_keys_or_exit(service)
    state = state_or_exit(state_url)
    providers = providers_for(service, live=False)
    document = discovery.file_snapshot(service, state, providers, out)
    discovery.write_snapshot(document, out)
    endpoints = sum(len(route["endpoints"]) for route in document["routes"])
    console.print(f"wrote {out}: {len(document['routes'])} routes, {endpoints} endpoints")


@app.command(help="Live view of router endpoints, circuits and traffic.")
def top(
    admin: Annotated[
        str, typer.Option("--admin", help="Router admin listener URL (/metrics, /debug/endpoints)")
    ] = topmod.DEFAULT_ADMIN,
    interval: Annotated[
        float, typer.Option("--interval", min=0.1, help="Seconds between polls")
    ] = topmod.DEFAULT_INTERVAL,
) -> None:
    topmod.run_top(admin, interval, console)


@app.command()
def doctor(path: SpecArg = DEFAULT_SPEC) -> None:
    service = load_or_exit(path)
    table = Table(title=f"doctor {service.name}")
    table.add_column("provider")
    table.add_column("type")
    table.add_column("credentials")
    table.add_column("detail")
    all_ok = True
    for target in service.targets:
        health = check_credentials(target)
        all_ok = all_ok and health.ok
        mark = "[green]OK[/green]" if health.ok else "[red]FAIL[/red]"
        table.add_row(target.provider, target.type, mark, health.message)
    console.print(table)
    if not all_ok:
        raise typer.Exit(1)


def check_credentials(target: specmod.TargetSpec) -> CredHealth:
    try:
        return provider_for(target).credentials_health()
    except Exception as exc:
        return CredHealth(ok=False, message=f"check failed: {exc}")


if __name__ == "__main__":
    app()
