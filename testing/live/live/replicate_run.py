from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from live.harness import LOG_DIR, REPO_ROOT, SNAPSHOT_PATH, SPEC_NAME, SPECS, STATE_PATH, run_logged
from multihull.providers.base import Ref
from multihull.state import LocalState

SPEC = SPECS / "replicate.yaml"
PROVIDER = "replicate"


def render(workdir: Path) -> dict[str, Any]:
    document = yaml.safe_load(SPEC.read_text())
    build = document["container"]["build"]
    build["context"] = str(REPO_ROOT / build["context"])
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / ".multihull").mkdir(exist_ok=True)
    (workdir / LOG_DIR).mkdir(exist_ok=True)
    (workdir / SPEC_NAME).write_text(yaml.safe_dump(document, sort_keys=False))
    return document


class ReplicateRun:
    def __init__(self, workdir: Path) -> None:
        self.workdir = workdir
        self.document = render(workdir)
        self.service = str(self.document["name"])

    @property
    def hostname(self) -> str:
        return str(self.document["route"]["hostname"])

    @property
    def snapshot_path(self) -> Path:
        return self.workdir / SNAPSHOT_PATH

    def run(self, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, "-m", "multihull.cli", *args]
        return run_logged(command, self.workdir / LOG_DIR / "hull.log", self.workdir, timeout)

    def hull(self, *args: str, timeout: float = 1800) -> subprocess.CompletedProcess[str]:
        return self.run(*args, "--state", str(self.workdir / STATE_PATH), timeout=timeout)

    def doctor(self) -> subprocess.CompletedProcess[str]:
        return self.run("doctor", SPEC_NAME, timeout=120)

    def deploy(self) -> subprocess.CompletedProcess[str]:
        return self.hull(
            "deploy", SPEC_NAME, "--apply", "--wait", "--snapshot-out", str(self.snapshot_path)
        )

    def destroy(self) -> subprocess.CompletedProcess[str]:
        return self.hull(
            "destroy", SPEC_NAME, "--yes", "--snapshot-out", str(self.snapshot_path), timeout=300
        )

    def record(self) -> Any:
        return LocalState(self.workdir / STATE_PATH).get(self.service, PROVIDER)

    def ref(self) -> Ref:
        record = self.record()
        if record is None:
            raise RuntimeError(f"no state record for {self.service}/{PROVIDER}")
        return Ref.from_json(record.ref)

    def last_status(self) -> str | None:
        record = self.record()
        return record.last_status if record else None

    def snapshot_endpoint(self) -> dict[str, Any]:
        document = json.loads(self.snapshot_path.read_text())
        [endpoint] = [e for route in document["routes"] for e in route["endpoints"]]
        return endpoint
