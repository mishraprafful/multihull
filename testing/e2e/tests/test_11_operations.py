from __future__ import annotations

from e2e.client import RouterClient, failures, load, providers_of
from e2e.harness import SERVICE, SPEC_NAME, STATE_PATH, Controller, Deployment, Router, hull
from e2e.waiting import wait_until
from multihull import engine
from multihull import spec as specmod


def plan_changes(deployment: Deployment) -> set[str]:
    spec = specmod.load(deployment.spec_path)
    return {plan.change for plan in engine.plan(spec, deployment.state())}


def test_second_deploy_is_a_no_op_plan(deployment: Deployment) -> None:
    result = hull(deployment.workdir, "deploy", SPEC_NAME, "--state", str(STATE_PATH))
    assert result.returncode == 0
    assert "unchanged" in result.stdout
    assert "dry run" in result.stdout
    assert plan_changes(deployment) == {"unchanged"}
    assert len(deployment.state().list(SERVICE)) == 3


def test_router_serves_the_last_snapshot_without_a_controller(
    controller: Controller, router: Router, client: RouterClient
) -> None:
    version_before = router.endpoints()
    controller.stop()
    try:
        outcomes = load(client, 20, stream=False, concurrency=4)
        assert failures(outcomes) == []
        assert providers_of(outcomes) == {"primary": 20}
        assert len(router.endpoints()) == len(version_before)
        assert router.healthz()
    finally:
        controller.start()
    router.wait_ready(30)
    assert providers_of(load(client, 5, stream=False, concurrency=1)) == {"primary": 5}


def test_lost_state_is_rebuilt_by_rediscover(
    deployment: Deployment, controller: Controller
) -> None:
    before = {record.provider: record for record in deployment.state().list(SERVICE)}
    controller.stop()
    try:
        (deployment.workdir / STATE_PATH).unlink()
        lock = (deployment.workdir / STATE_PATH).with_suffix(".lock")
        if lock.exists():
            lock.unlink()

        result = hull(deployment.workdir, "status", SPEC_NAME, "--state", str(STATE_PATH))
        assert result.returncode == 0, result.stderr
        assert "rebuilt state from rediscover" in result.stdout
        for name in ("primary", "secondary", "tertiary"):
            assert name in result.stdout

        after = {record.provider: record for record in deployment.state().list(SERVICE)}
        assert set(after) == set(before)
        for name, record in after.items():
            assert record.ref == before[name].ref
            assert record.spec_hash == before[name].spec_hash
            assert record.last_status == "Ready"
        assert plan_changes(deployment) == {"unchanged"}
    finally:
        controller.start()
    wait_until(controller.snapshot_ready, 30, message="controller snapshot after rebuild")
