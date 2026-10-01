from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
import yaml
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from multihull import discovery, engine
from multihull import spec as specmod
from multihull.providers import create
from multihull.providers.base import CredHealth, Provider, Ref
from multihull.state import LocalState

app = typer.Typer(
    name="hull",
    help="Deploy always-warm GPU inference containers to many providers from one spec.",
    no_args_is_help=True,
)
console = Console()
errors = Console(stderr=True)

SpecArg = Annotated[Path, typer.Argument(help="Path to multihull.yaml")]
DEFAULT_SPEC = Path("multihull.yaml")
DEFAULT_STATE = Path(".multihull/state.db")
DEFAULT_IMAGE = "ghcr.io/ORG/IMAGE:TAG"


def load_or_exit(path: Path) -> specmod.ServiceSpec:
    try:
        return specmod.load(path)
    except FileNotFoundError:
        errors.print(f"[red]{path} not found[/red]")
        raise typer.Exit(2) from None
    except (ValidationError, ValueError) as exc:
        errors.print(f"[red]{path} is invalid[/red]\n{exc}")
        raise typer.Exit(1) from None


def provider_for(target: specmod.TargetSpec) -> Provider:
    if target.type == "kubernetes":
        context = target.kubernetes.context if target.kubernetes else None
        return create("kubernetes", context=context)
    return create(target.type)


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
    service = load_or_exit(path)
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
    state_path: Annotated[Path, typer.Option("--state", help="State database")] = DEFAULT_STATE,
) -> None:
    service = load_or_exit(path)
    state = LocalState(state_path)
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


def colour_change(change: str) -> str:
    colours = {"new": "green", "changed": "yellow", "unchanged": "dim", "orphaned": "red"}
    return f"[{colours[change]}]{change}[/{colours[change]}]"


@app.command()
def status(
    path: SpecArg = DEFAULT_SPEC,
    state_path: Annotated[Path, typer.Option("--state", help="State database")] = DEFAULT_STATE,
) -> None:
    service = load_or_exit(path)
    state = LocalState(state_path)
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
def snapshot(
    path: SpecArg = DEFAULT_SPEC,
    out: Annotated[Path, typer.Option(help="Snapshot file")] = Path("snapshot.json"),
    state_path: Annotated[Path, typer.Option("--state", help="State database")] = DEFAULT_STATE,
) -> None:
    service = load_or_exit(path)
    state = LocalState(state_path)
    providers = {t.provider: provider_for(t) for t in service.targets}
    document = discovery.build_snapshot(service, state, providers)
    discovery.write_snapshot(document, out)
    endpoints = sum(len(route["endpoints"]) for route in document["routes"])
    console.print(f"wrote {out}: {len(document['routes'])} routes, {endpoints} endpoints")


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
