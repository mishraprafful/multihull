from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Collection, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

from multihull.providers import create
from multihull.providers.base import Observed, Plan, Provider, Ref, Target, error_reason
from multihull.spec import ServiceSpec, TargetSpec
from multihull.state.base import StateBackend, StateRecord

Change = Literal["new", "changed", "unchanged", "orphaned", "skipped"]
ProviderFactory = Callable[[TargetSpec], Provider]
RefProviderFactory = Callable[[Ref], Provider]
DEFAULT_PLAN_DIR = Path(".multihull/plan")
APPLIED_CHANGES = {"new", "changed", "unchanged"}


@dataclass
class TargetPlan:
    provider: str
    type: str
    change: Change
    spec_hash: str
    plan: Plan | None
    ref: Ref | None = None


@dataclass
class ApplyResult:
    provider: str
    type: str
    change: Change
    ok: bool
    ref: Ref | None = None
    message: str = ""


@dataclass
class DestroyResult:
    provider: str
    type: str
    ok: bool
    message: str = ""


@dataclass
class RefreshResult:
    provider: str
    type: str
    ref: Ref
    observed: Observed
    changed: bool


@dataclass
class RediscoverResult:
    provider: str
    type: str
    ref: Ref | None
    message: str


def provider_kwargs(target: TargetSpec, live: bool) -> dict[str, Any]:
    if target.type == "kubernetes":
        context = target.kubernetes.context if target.kubernetes else None
        return {"context": context, "connect": live}
    if target.type == "modal":
        return {"dry_run": not live, "provider_name": target.provider}
    if target.type == "docker":
        return {"provider_name": target.provider}
    return {}


def provider_for(target: TargetSpec, live: bool = False) -> Provider:
    return create(target.type, **provider_kwargs(target, live))


def live_provider_for(target: TargetSpec) -> Provider:
    return provider_for(target, live=True)


def default_factory(target: TargetSpec) -> Provider:
    return provider_for(target, live=False)


def provider_for_ref(ref: Ref) -> Provider:
    if ref.type == "kubernetes":
        return create("kubernetes", context=ref.ids.get("context"), connect=True)
    if ref.type == "modal":
        return create("modal", dry_run=False)
    return create(ref.type)


def resolve_providers(
    spec: ServiceSpec, providers: Mapping[str, Provider] | None, factory: ProviderFactory
) -> dict[str, Provider]:
    resolved: dict[str, Provider] = {}
    for target in spec.targets:
        if providers is not None and target.provider in providers:
            resolved[target.provider] = providers[target.provider]
        else:
            resolved[target.provider] = factory(target)
    return resolved


def payload_hash(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def plan(
    spec: ServiceSpec,
    state: StateBackend,
    providers: Mapping[str, Provider] | None = None,
    factory: ProviderFactory = default_factory,
    image_digest: str | None = None,
) -> list[TargetPlan]:
    resolved = resolve_providers(spec, providers, factory)
    records = {r.provider: r for r in state.list(spec.name)}
    plans: list[TargetPlan] = []
    for target in sorted(spec.targets, key=lambda t: t.priority):
        record = records.pop(target.provider, None)
        ref = Ref.from_json(record.ref) if record else None
        rendered = resolved[target.provider].plan(Target(spec, target, image_digest), ref)
        digest = payload_hash(rendered.payload)
        if record is None:
            change: Change = "new"
        elif record.spec_hash != digest:
            change = "changed"
        else:
            change = "unchanged"
        plans.append(TargetPlan(target.provider, target.type, change, digest, rendered, ref))
    for record in records.values():
        ref = Ref.from_json(record.ref)
        plans.append(TargetPlan(record.provider, ref.type, "orphaned", record.spec_hash, None, ref))
    return plans


def apply(
    spec: ServiceSpec,
    state: StateBackend,
    dry_run: bool = True,
    providers: Mapping[str, Provider] | None = None,
    factory: ProviderFactory = default_factory,
    image_digest: str | None = None,
    max_workers: int = 8,
    only: Collection[str] | None = None,
) -> list[ApplyResult]:
    resolved = resolve_providers(spec, providers, factory)
    plans = plan(spec, state, resolved, factory, image_digest)
    targets = {t.provider: t for t in spec.targets}

    def run(target_plan: TargetPlan) -> ApplyResult:
        if target_plan.change == "orphaned":
            return ApplyResult(
                target_plan.provider,
                target_plan.type,
                "orphaned",
                True,
                target_plan.ref,
                "not in spec; run destroy to remove",
            )
        if only is not None and target_plan.provider not in only:
            return ApplyResult(
                target_plan.provider,
                target_plan.type,
                "skipped",
                True,
                target_plan.ref,
                "not selected",
            )
        if dry_run:
            return ApplyResult(
                target_plan.provider,
                target_plan.type,
                target_plan.change,
                True,
                target_plan.ref,
                "dry run",
            )
        target = targets[target_plan.provider]
        provider = resolved[target.provider]
        try:
            ref = provider.apply(Target(spec, target, image_digest), target_plan.ref)
        except Exception as exc:
            return ApplyResult(
                target.provider, target.type, target_plan.change, False, None, error_reason(exc)
            )
        return ApplyResult(target.provider, target.type, target_plan.change, True, ref, "applied")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(run, plans))

    if not dry_run:
        with state.lock():
            for result, target_plan in zip(results, plans, strict=True):
                if result.ok and result.ref is not None and result.change in APPLIED_CHANGES:
                    state.put(
                        StateRecord(
                            service=spec.name,
                            provider=result.provider,
                            ref=result.ref.to_json(),
                            image_digest=image_digest,
                            spec_hash=target_plan.spec_hash,
                            last_status="Pending",
                        )
                    )
    return results


def write_plan_dir(plans: list[TargetPlan], out_dir: str | Path = DEFAULT_PLAN_DIR) -> list[Path]:
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.*"):
        if stale.suffix in {".yaml", ".json"}:
            stale.unlink()
    written: list[Path] = []
    for target_plan in plans:
        if target_plan.plan is None:
            continue
        payload = target_plan.plan.payload
        if target_plan.plan.format == "yaml":
            path = directory / f"{target_plan.provider}.yaml"
            documents = payload if isinstance(payload, list) else [payload]
            path.write_text(yaml.safe_dump_all(documents, sort_keys=False))
        else:
            path = directory / f"{target_plan.provider}.json"
            path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
        written.append(path)
    return written


def destroy(
    service: str,
    state: StateBackend,
    providers: Mapping[str, Provider] | None = None,
    factory: RefProviderFactory = provider_for_ref,
    only: Collection[str] | None = None,
    max_workers: int = 8,
) -> list[DestroyResult]:
    records = [r for r in state.list(service) if only is None or r.provider in only]

    def run(record: StateRecord) -> DestroyResult:
        ref = Ref.from_json(record.ref)
        try:
            provider = (
                providers[record.provider]
                if providers is not None and record.provider in providers
                else factory(ref)
            )
            provider.destroy(ref)
        except Exception as exc:
            return DestroyResult(record.provider, ref.type, False, error_reason(exc))
        return DestroyResult(record.provider, ref.type, True, "destroyed")

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(run, records))

    with state.lock():
        for result in results:
            if result.ok:
                state.delete(service, result.provider)
    return results


def rediscover(
    spec: ServiceSpec,
    state: StateBackend,
    providers: Mapping[str, Provider],
    image_digest: str | None = None,
) -> list[RediscoverResult]:
    known = {record.provider for record in state.list(spec.name)}
    results: list[RediscoverResult] = []
    for target in sorted(spec.targets, key=lambda t: t.priority):
        if target.provider in known or target.provider not in providers:
            continue
        provider = providers[target.provider]
        try:
            ref = provider.rediscover(spec.name)
        except Exception as exc:
            results.append(RediscoverResult(target.provider, target.type, None, error_reason(exc)))
            continue
        if ref is None or ref.provider != target.provider:
            results.append(RediscoverResult(target.provider, target.type, None, "nothing found"))
            continue
        rendered = provider.plan(Target(spec, target, image_digest), ref)
        with state.lock():
            state.put(
                StateRecord(
                    service=spec.name,
                    provider=target.provider,
                    ref=ref.to_json(),
                    image_digest=image_digest,
                    spec_hash=payload_hash(rendered.payload),
                    last_status="Unknown",
                )
            )
        results.append(RediscoverResult(target.provider, target.type, ref, "rediscovered"))
    return results


def refresh(
    service: str,
    state: StateBackend,
    providers: Mapping[str, Provider] | None = None,
    factory: RefProviderFactory = provider_for_ref,
    max_workers: int = 8,
) -> list[RefreshResult]:
    records = state.list(service)

    def run(record: StateRecord) -> RefreshResult:
        ref = Ref.from_json(record.ref)
        try:
            provider = (
                providers[record.provider]
                if providers is not None and record.provider in providers
                else factory(ref)
            )
            observed = provider.status(ref)
        except Exception as exc:
            observed = Observed(phase="Unknown", message=error_reason(exc))
        return RefreshResult(
            record.provider, ref.type, ref, observed, observed.phase != record.last_status
        )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        results = list(pool.map(run, records))

    with state.lock():
        for record, result in zip(records, results, strict=True):
            if result.changed:
                record.last_status = result.observed.phase
                state.put(record)
    return results
