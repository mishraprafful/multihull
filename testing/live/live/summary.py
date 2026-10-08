from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

SUMMARY_JSON = "summary.json"
SUMMARY_MD = "summary.md"
DETAILS_THRESHOLD = 20
MESSAGE_LIMIT = 160
CELL_LIMIT = 120
WORKFLOW_DESTROY_LOG = Path("logs") / "workflow-destroy.log"
BACKSTOP_LOG = Path("logs") / "backstop.log"
BACKSTOP_RESULT = re.compile(r"(\d+) app\(s\) matched (\S+), (\d+) failed")
OUTCOME_LABELS = {"passed": "pass", "failed": "fail", "skipped": "skipped", "error": "error"}


class Scenario(BaseModel):
    nodeid: str
    name: str
    outcome: str = "running"
    duration: float = 0.0
    requests: int = 0
    client_errors: int = 0
    providers: dict[str, int] = Field(default_factory=dict)
    failovers: int | None = None
    note: str = ""
    message: str = ""


class Pod(BaseModel):
    name: str
    phase: str
    ready: str
    restarts: int
    image: str
    image_id: str
    node: str


class KubeSnapshot(BaseModel):
    label: str
    namespace: str
    wide: str = ""
    rollout: str = ""
    pods: list[Pod] = Field(default_factory=list)
    node_port: int | None = None
    router_url: str | None = None
    events: list[dict[str, str]] = Field(default_factory=list)
    describe: str = ""
    error: str = ""


class ModalState(BaseModel):
    label: str
    state: str
    tasks: int | None = None
    at: str = ""
    error: str = ""


class ImageBuild(BaseModel):
    image_id: str
    log: str
    tail: str


class ModalInfo(BaseModel):
    app_name: str
    environment: str
    provider: str = ""
    app_id: str | None = None
    web_url: str | None = None
    dashboard_url: str | None = None
    states: list[ModalState] = Field(default_factory=list)
    image_builds: list[ImageBuild] = Field(default_factory=list)


class GpuCost(BaseModel):
    gpu: str
    targets: int
    hourly_usd: float
    price_date: str
    price_source: str
    budget_usd: float
    max_minutes: int
    started_at: float | None = None
    stopped_at: float | None = None

    def wall_clock_minutes(self, now: float) -> float:
        if self.started_at is None:
            return 0.0
        return max(0.0, (self.stopped_at or now) - self.started_at) / 60

    def gpu_minutes(self, now: float) -> float:
        return self.wall_clock_minutes(now) * self.targets

    def estimate_usd(self, now: float) -> float:
        return self.gpu_minutes(now) / 60 * self.hourly_usd


class RouterState(BaseModel):
    label: str
    snapshot_version: int | None = None
    endpoints: list[dict[str, Any]] = Field(default_factory=list)
    requests_by_outcome: dict[str, float] = Field(default_factory=dict)
    failovers_by_reason: dict[str, float] = Field(default_factory=dict)
    error: str = ""


class RunSummary(BaseModel):
    spec: str
    service: str
    started_at: float = Field(default_factory=time.time)
    finished_at: float | None = None
    exit_status: int | None = None
    scenarios: list[Scenario] = Field(default_factory=list)
    kube: list[KubeSnapshot] = Field(default_factory=list)
    modal: ModalInfo | None = None
    modal_apps: list[ModalInfo] = Field(default_factory=list)
    cost: GpuCost | None = None
    router: RouterState | None = None
    destroy: str = ""

    def scenario(self, nodeid: str, name: str) -> Scenario:
        for existing in self.scenarios:
            if existing.nodeid == nodeid:
                return existing
        created = Scenario(nodeid=nodeid, name=name)
        self.scenarios.append(created)
        return created

    def has_kube(self, label: str) -> bool:
        return any(snapshot.label == label for snapshot in self.kube)

    def has_modal_state(self, label: str) -> bool:
        return self.modal is not None and any(s.label == label for s in self.modal.states)

    def modal_app(self, provider: str) -> ModalInfo | None:
        return next((info for info in self.modal_apps if info.provider == provider), None)

    def save(self, workdir: Path) -> None:
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / SUMMARY_JSON).write_text(self.model_dump_json(indent=2))
        (workdir / SUMMARY_MD).write_text(render(self))

    @classmethod
    def load(cls, workdir: Path) -> RunSummary | None:
        path = workdir / SUMMARY_JSON
        if not path.exists():
            return None
        return cls.model_validate_json(path.read_text())


class Crash(Protocol):
    message: str


class PytestReport(Protocol):
    nodeid: str
    when: str
    outcome: str
    duration: float
    longrepr: Any


def one_line(text: str, limit: int = MESSAGE_LIMIT) -> str:
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return line if len(line) <= limit else line[: limit - 3] + "..."


def failure_message(report: PytestReport) -> str:
    crash: Crash | None = getattr(report.longrepr, "reprcrash", None)
    if crash is not None:
        return one_line(crash.message)
    if isinstance(report.longrepr, tuple):
        return one_line(str(report.longrepr[-1]))
    return one_line(str(report.longrepr or ""))


def apply_report(scenario: Scenario, report: PytestReport) -> None:
    xfail = hasattr(report, "wasxfail")
    if report.when == "call":
        scenario.duration = report.duration
        if xfail:
            scenario.outcome = "xfail" if report.outcome == "skipped" else "xpass"
        else:
            scenario.outcome = OUTCOME_LABELS.get(report.outcome, report.outcome)
    elif report.outcome == "skipped":
        scenario.outcome = "xfail" if xfail else "skipped"
    elif report.outcome == "failed":
        scenario.outcome = (
            "error" if report.when == "setup" or scenario.outcome == "pass" else "fail"
        )
    if report.outcome == "failed" or (report.outcome == "skipped" and not xfail):
        scenario.message = scenario.message or failure_message(report)


def cell(value: object, limit: int = CELL_LIMIT) -> str:
    text = " ".join(str(value).split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in rows)
    return lines


def details(title: str, body: list[str], always: bool = False) -> list[str]:
    if not always and len(body) <= DETAILS_THRESHOLD:
        return [f"**{title}**", "", *body, ""]
    return [f"<details><summary>{title}</summary>", "", *body, "", "</details>", ""]


def fenced(text: str) -> list[str]:
    return ["```", *(text.rstrip().splitlines() or ["(empty)"]), "```"]


def duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f} s"
    minutes, rest = divmod(int(round(seconds)), 60)
    return f"{minutes} m {rest} s"


def providers_cell(providers: dict[str, int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in sorted(providers.items())) or "-"


def overall(summary: RunSummary, job_status: str | None) -> str:
    failed = [s for s in summary.scenarios if s.outcome in {"fail", "error", "xpass"}]
    if job_status == "cancelled":
        return "CANCELLED"
    if summary.exit_status is None:
        return "INCOMPLETE" if not failed else "FAIL"
    if summary.exit_status != 0 or failed or job_status == "failure":
        return "FAIL"
    return "PASS"


def render_glance(summary: RunSummary, job_status: str | None) -> list[str]:
    end = summary.finished_at or time.time()
    counts: dict[str, int] = {}
    for scenario in summary.scenarios:
        counts[scenario.outcome] = counts.get(scenario.outcome, 0) + 1
    tally = ", ".join(f"{count} {outcome}" for outcome, count in sorted(counts.items()))
    lines = [
        "### Result at a glance",
        "",
        f"**{overall(summary, job_status)}** in {duration(end - summary.started_at)}"
        f" ({tally or 'no scenarios ran'})",
        "",
    ]
    rows = [
        [
            s.name,
            s.outcome,
            duration(s.duration),
            s.requests or "-",
            s.client_errors if s.requests else "-",
            providers_cell(s.providers),
            "-" if s.failovers is None else s.failovers,
            s.message or s.note or "",
        ]
        for s in summary.scenarios
    ]
    headers = [
        "Scenario",
        "Outcome",
        "Duration",
        "Requests",
        "Client errors",
        "Served by",
        "Failovers",
        "Note",
    ]
    return [*lines, *table(headers, rows), ""]


def render_kube(snapshot: KubeSnapshot) -> list[str]:
    lines = [f"#### {snapshot.label}", ""]
    if snapshot.error:
        return [*lines, f"Capture failed: {cell(snapshot.error)}", ""]
    lines.append(f"- Rollout: {cell(snapshot.rollout) or '-'}")
    lines.append(
        f"- NodePort: {snapshot.node_port or '-'}, router URL: {snapshot.router_url or '-'}"
    )
    lines.append("")
    if snapshot.pods:
        rows = [
            [p.name, p.phase, p.ready, p.restarts, p.image, p.image_id or "-", p.node]
            for p in snapshot.pods
        ]
        headers = ["Pod", "Phase", "Ready", "Restarts", "Image", "Image ID", "Node"]
        lines.extend([*table(headers, rows), ""])
    else:
        lines.extend(["No pods.", ""])
    lines.extend(details(f"kubectl get -o wide ({snapshot.namespace})", fenced(snapshot.wide)))
    if snapshot.events:
        rows = [
            [
                e.get("time", ""),
                e.get("type", ""),
                e.get("reason", ""),
                e.get("object", ""),
                e.get("message", ""),
            ]
            for e in snapshot.events
        ]
        events = table(["Time (UTC)", "Type", "Reason", "Object", "Message"], rows)
        lines.extend(details(f"Last {len(snapshot.events)} events", events, always=True))
    if snapshot.describe:
        lines.extend(details("kubectl describe of non-ready pods", fenced(snapshot.describe), True))
    return lines


def render_modal(info: ModalInfo, cleanup: list[str]) -> list[str]:
    app_id = f"`{info.app_id}`" if info.app_id else "-"
    endpoint = f"[{info.web_url}]({info.web_url})" if info.web_url else "-"
    if info.dashboard_url:
        dashboard = f"[dashboard]({info.dashboard_url})"
    elif info.app_id:
        dashboard = f"`modal app logs {info.app_id} --env {info.environment}`"
    else:
        dashboard = "-"
    lines = [
        f"### Modal ({info.provider})" if info.provider else "### Modal",
        "",
        *table(
            ["App", "App id", "Environment", "Endpoint", "Dashboard"],
            [[info.app_name, app_id, info.environment, endpoint, dashboard]],
        ),
        "",
    ]
    if info.states:
        rows = [
            [s.label, s.state, "-" if s.tasks is None else s.tasks, s.error or s.at]
            for s in info.states
        ]
        lines.extend([*table(["When", "State", "Containers", "Detail"], rows), ""])
    for build in info.image_builds:
        count = len(build.tail.splitlines())
        lines.extend(
            [
                f"**Image build `{build.image_id}` failed.** Last {count} lines of "
                f"`modal image logs {build.image_id}`; the full log is `{build.log}` in the "
                "artifact.",
                "",
                *fenced(build.tail),
                "",
            ]
        )
    if cleanup:
        lines.extend(["**Cleanup**", "", *(f"- {line}" for line in cleanup), ""])
    return lines


def render_router(state: RouterState) -> list[str]:
    lines = ["### Router", ""]
    if state.error:
        return [*lines, f"Capture failed: {cell(state.error)}", ""]
    lines.extend([f"Snapshot version {state.snapshot_version}, captured {state.label}.", ""])
    rows = [
        [
            e.get("id", ""),
            e.get("provider", ""),
            e.get("health", ""),
            e.get("circuit") or "-",
            (e.get("probe") or {}).get("state") or "-",
            (e.get("probe") or {}).get("last_status") or "-",
        ]
        for e in state.endpoints
    ]
    headers = ["Endpoint", "Provider", "Health", "Circuit", "Probe", "Last probe status"]
    lines.extend([*table(headers, rows), ""])
    outcomes = [[k, f"{v:.0f}"] for k, v in sorted(state.requests_by_outcome.items())]
    reasons = [[k, f"{v:.0f}"] for k, v in sorted(state.failovers_by_reason.items())]
    lines.extend([*table(["router_requests_total outcome", "Count"], outcomes or [["-", 0]]), ""])
    lines.extend([*table(["router_failovers_total reason", "Count"], reasons or [["-", 0]]), ""])
    return lines


def render_cost(cost: GpuCost, now: float) -> list[str]:
    lines = ["### Cost", ""]
    if cost.started_at is None:
        lines.append(
            f"No GPU container started, so only Modal's image build was billed. "
            f"Budget: {cost.budget_usd:.0f} USD per run."
        )
        return [*lines, ""]
    wall = cost.wall_clock_minutes(now)
    estimate = cost.estimate_usd(now)
    verdict = "within" if estimate <= cost.budget_usd else "OVER"
    lines.extend(
        [
            f"About {estimate:.2f} USD: {cost.targets} x {cost.gpu} for {wall:.1f} wall-clock "
            f"minutes from deploy to destroy ({cost.gpu_minutes(now):.1f} GPU minutes) at "
            f"{cost.hourly_usd:.2f} USD per GPU hour ({cost.price_source}, read "
            f"{cost.price_date}). CPU, memory and the image build are extra, cents each.",
            "",
            f"Budget: {cost.budget_usd:.0f} USD per run, {verdict} budget. The harness aborts "
            f"and destroys after {cost.max_minutes} minutes (`max_minutes`).",
        ]
    )
    return [*lines, ""]


def render(
    summary: RunSummary,
    job_status: str | None = None,
    cleanup: list[str] | None = None,
    artifact: str | None = None,
    now: float | None = None,
) -> str:
    lines = [f"## Live suite: {summary.spec} ({summary.service})", ""]
    lines.extend(render_glance(summary, job_status))
    if summary.cost is not None:
        lines.extend(render_cost(summary.cost, now or time.time()))
    if summary.kube or summary.cost is None:
        lines.extend(["### Kubernetes (kind)", ""])
        for snapshot in summary.kube:
            lines.extend(render_kube(snapshot))
        if not summary.kube:
            lines.extend(["No state captured.", ""])
    destroy = [f"Suite destroy: {summary.destroy}"] if summary.destroy else []
    cleanup_lines = [*destroy, *(cleanup or [])]
    apps = [*([summary.modal] if summary.modal is not None else []), *summary.modal_apps]
    for index, info in enumerate(apps):
        lines.extend(render_modal(info, cleanup_lines if index == len(apps) - 1 else []))
    if not apps and cleanup_lines:
        lines.extend(["### Cleanup", "", *(f"- {line}" for line in cleanup_lines), ""])
    if summary.router is not None:
        lines.extend(render_router(summary.router))
    if artifact:
        lines.extend(["### Artifacts", "", artifact, ""])
    return "\n".join(lines).rstrip() + "\n"


def log_text(path: Path) -> str | None:
    return path.read_text(errors="replace") if path.exists() else None


def destroy_line(text: str) -> str:
    if "nothing to destroy" in text:
        return "nothing left in state; the suite had already destroyed everything"
    if "no rendered spec" in text:
        return "no spec was rendered, so nothing was deployed"
    if "failed" in text.lower():
        return "failed; see logs/workflow-destroy.log in the artifact"
    return "destroyed resources the suite left behind"


def backstop_line(text: str) -> str:
    if text.startswith("skipped, credentials"):
        return text.strip()
    match = BACKSTOP_RESULT.search(text)
    if match is None:
        return "did not report a result; see logs/backstop.log"
    matched, prefix, failed = int(match[1]), match[2], int(match[3])
    if matched == 0:
        return f"not needed, no running app matched {prefix}"
    return f"had to act: stopped {matched - failed} of {matched} app(s) matching {prefix}"


def workflow_cleanup(workdir: Path) -> list[str]:
    lines: list[str] = []
    destroyed = log_text(workdir / WORKFLOW_DESTROY_LOG)
    if destroyed is not None:
        lines.append(f"Workflow destroy step: {destroy_line(destroyed)}")
    backstop = log_text(workdir / BACKSTOP_LOG)
    if backstop is not None:
        lines.append(f"Backstop `modal app stop`: {backstop_line(backstop)}")
    return lines


def artifact_line(name: str) -> str:
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if repository and run_id:
        return (
            f"Logs: artifact `{name}` on [this run]({server}/{repository}/actions/runs/{run_id})."
        )
    return f"Logs: artifact `{name}`."


def publish(
    workdir: Path, job_status: str | None, artifact: str | None, spec: str, service: str
) -> str:
    summary = RunSummary.load(workdir) or RunSummary(spec=spec, service=service)
    if summary.cost is not None and summary.cost.started_at and not summary.cost.stopped_at:
        summary.cost.stopped_at = time.time()
    failed = job_status not in (None, "success") or overall(summary, job_status) != "PASS"
    text = render(
        summary,
        job_status,
        workflow_cleanup(workdir),
        artifact_line(artifact) if artifact and failed else None,
    )
    (workdir / SUMMARY_MD).parent.mkdir(parents=True, exist_ok=True)
    (workdir / SUMMARY_MD).write_text(text)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as handle:
            handle.write(text)
    return text


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the live suite job summary")
    parser.add_argument("command", choices=["publish"])
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--job-status", default=None)
    parser.add_argument("--artifact", default=None)
    parser.add_argument("--spec", default=os.environ.get("LIVE_SPEC", "kind-docker"))
    parser.add_argument("--service", default=os.environ.get("LIVE_SERVICE", "live-local"))
    args = parser.parse_args(argv)
    publish(args.workdir, args.job_status, args.artifact, args.spec, args.service)
    return 0


if __name__ == "__main__":
    sys.exit(main())
