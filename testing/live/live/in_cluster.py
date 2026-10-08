from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from e2e.harness import Process, free_port
from e2e.stream import new_ca, new_leaf, write_cert, write_key
from live.harness import LOG_DIR, REPO_ROOT, SPECS, run_logged

DEFAULT_CLUSTER = "multihull-in-cluster"
SPEC_FILE = SPECS / "in-cluster.yaml"
SPEC_NAME = "multihull.yaml"
PROVIDER = "kind"
RELEASE = "multihull"
RELEASE_NAMESPACE = "multihull-system"
SERVICE_ACCOUNT = RELEASE
CONTROLLER = f"{RELEASE}-controller"
ROUTER = f"{RELEASE}-router"
GRPC_PORT = 9443
ROUTER_LISTEN = 8080
ROUTER_ADMIN = 9090
HOME = "/var/lib/multihull"
STATE_FILE = f"{HOME}/state/state.db"
STATE_URL = f"sqlite://{STATE_FILE}"
DEFAULT_STATE_FILE = f"{HOME}/.multihull/state.db"
SPEC_MOUNT = "/etc/multihull/multihull.yaml"
CLUSTER_ROLE = "multihull-in-cluster-controller"
STATE_SECRET = "multihull-state"
STATE_CLAIM = "multihull-state"
SPEC_CONFIG_MAP = "multihull-spec"
CONTROLLER_TLS_SECRET = "multihull-controller-tls"
CLIENT_CA_SECRET = "multihull-discovery-ca"
TOKEN_SECRET = "multihull-discovery-token"
ROUTER_DISCOVERY_SECRET = "multihull-router-discovery"
LOCAL_IMAGES = {
    "controller": ("multihull-controller:in-cluster", "python", "python/Dockerfile"),
    "router": ("multihull-router:in-cluster", ".", "router/Dockerfile"),
    "mock": ("multihull-mock-server:live", "testing/mock-server", "testing/mock-server/Dockerfile"),
}
CONTROLLER_RULES = [
    {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get", "list", "patch"]},
    {"apiGroups": [""], "resources": ["services", "nodes"], "verbs": ["get", "list"]},
    {
        "apiGroups": ["autoscaling"],
        "resources": ["horizontalpodautoscalers"],
        "verbs": ["get", "patch"],
    },
]


@dataclass(frozen=True)
class Settings:
    cluster: str
    service: str
    images: dict[str, str | None]
    workdir: Path | None
    helm: str

    @classmethod
    def from_env(cls) -> Settings:
        run_id = os.environ.get("GITHUB_RUN_ID")
        workdir = os.environ.get("LIVE_WORKDIR")
        return cls(
            cluster=os.environ.get("LIVE_KIND_CLUSTER", DEFAULT_CLUSTER),
            service=f"in-cluster-{run_id}" if run_id else "in-cluster-local",
            images={
                "controller": os.environ.get("LIVE_CONTROLLER_IMAGE"),
                "router": os.environ.get("LIVE_ROUTER_IMAGE"),
                "mock": os.environ.get("LIVE_MOCK_IMAGE"),
            },
            workdir=Path(workdir).resolve() if workdir else None,
            helm=os.environ.get("HELM", "helm"),
        )

    @property
    def context(self) -> str:
        return f"kind-{self.cluster}"


@dataclass(frozen=True)
class Credentials:
    ca: Path
    server_cert: Path
    server_key: Path
    client_cert: Path
    client_key: Path
    token: Path


def controller_names(namespace: str) -> list[x509.GeneralName]:
    hosts = [CONTROLLER, f"{CONTROLLER}.{namespace}", f"{CONTROLLER}.{namespace}.svc"]
    hosts.append(f"{CONTROLLER}.{namespace}.svc.cluster.local")
    return [x509.DNSName(host) for host in hosts]


def generate_credentials(directory: Path, namespace: str) -> Credentials:
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    ca = new_ca("multihull in-cluster ca")
    server = new_leaf(
        "hull controller", ca, ExtendedKeyUsageOID.SERVER_AUTH, controller_names(namespace)
    )
    client = new_leaf("multihull router", ca, ExtendedKeyUsageOID.CLIENT_AUTH, [])
    token = directory / "token"
    token.touch(mode=0o600)
    token.write_text(secrets.token_urlsafe(32))
    return Credentials(
        ca=write_cert(directory / "ca.crt", ca[0]),
        server_cert=write_cert(directory / "controller.crt", server[0]),
        server_key=write_key(directory / "controller.key", server[1]),
        client_cert=write_cert(directory / "router.crt", client[0]),
        client_key=write_key(directory / "router.key", client[1]),
        token=token,
    )


def split_image(image: str) -> tuple[str, str]:
    repository, _, tag = image.rpartition(":")
    if not repository or "/" in tag:
        raise ValueError(f"image {image!r} needs an explicit tag")
    return repository, tag


def workload_spec(service: str, context: str | None) -> dict[str, Any]:
    document = yaml.safe_load(SPEC_FILE.read_text())
    document["name"] = service
    if context:
        for target in document["targets"]:
            target["kubernetes"]["context"] = context
    return document


def state_claim_manifest() -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": STATE_CLAIM, "namespace": RELEASE_NAMESPACE},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "64Mi"}},
        },
    }


def cluster_role_manifest() -> dict[str, Any]:
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": CLUSTER_ROLE},
        "rules": CONTROLLER_RULES,
    }
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": CLUSTER_ROLE},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": CLUSTER_ROLE,
        },
        "subjects": [
            {"kind": "ServiceAccount", "name": SERVICE_ACCOUNT, "namespace": RELEASE_NAMESPACE}
        ],
    }
    return {"apiVersion": "v1", "kind": "List", "items": [role, binding]}


class InCluster:
    def __init__(self, settings: Settings, workdir: Path) -> None:
        self.settings = settings
        self.workdir = workdir
        self.logs = workdir / LOG_DIR
        self.logs.mkdir(parents=True, exist_ok=True)
        self.inputs = workdir / "in-cluster"
        self.inputs.mkdir(exist_ok=True)
        self.workload = workload_spec(settings.service, settings.context)
        self.namespace = str(self.workload["targets"][0]["kubernetes"]["namespace"])
        self.images: dict[str, str] = {}

    def kubectl(
        self, *args: str, namespace: str | None = None, timeout: float = 300
    ) -> subprocess.CompletedProcess[str]:
        scope = ["--namespace", namespace] if namespace else []
        command = ["kubectl", "--context", self.settings.context, *scope, *args]
        return run_logged(command, self.logs / "kubectl.log", timeout=timeout)

    def kubectl_ok(self, *args: str, namespace: str | None = None) -> str:
        result = self.kubectl(*args, namespace=namespace)
        if result.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args[:2])} failed: {result.stderr}")
        return result.stdout

    def helm(self, *args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        command = [self.settings.helm, "--kube-context", self.settings.context, *args]
        return run_logged(command, self.logs / "helm.log", timeout=timeout)

    def hull(self, *args: str, timeout: float = 600) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            "-m",
            "multihull.cli",
            *args,
            "--state",
            str(self.workdir / ".multihull" / "state.db"),
        ]
        return run_logged(command, self.logs / "hull.log", cwd=self.workdir, timeout=timeout)

    def reachable(self) -> bool:
        return self.kubectl("get", "nodes", "-o", "name", timeout=30).returncode == 0

    def prepare_images(self) -> None:
        for name, (local, context, dockerfile) in LOCAL_IMAGES.items():
            image = self.settings.images[name]
            if image is None:
                image = local
                build = ["docker", "build", "-q", "-t", image, "-f", dockerfile, context]
                result = run_logged(build, self.logs / "docker.log", cwd=REPO_ROOT, timeout=1800)
                if result.returncode != 0:
                    raise RuntimeError(f"docker build {image} failed: {result.stderr}")
            load = ["kind", "load", "docker-image", image, "--name", self.settings.cluster]
            result = run_logged(load, self.logs / "docker.log", timeout=600)
            if result.returncode != 0:
                raise RuntimeError(f"kind load {image} failed: {result.stderr}")
            self.images[name] = image

    def reset(self) -> None:
        self.helm("uninstall", RELEASE, "--namespace", RELEASE_NAMESPACE, "--wait")
        for namespace in (RELEASE_NAMESPACE, self.namespace):
            self.kubectl("delete", "namespace", namespace, "--ignore-not-found", "--wait=true")
        for kind in ("clusterrolebinding", "clusterrole"):
            self.kubectl("delete", kind, CLUSTER_ROLE, "--ignore-not-found")
        for namespace in (RELEASE_NAMESPACE, self.namespace):
            self.kubectl_ok("create", "namespace", namespace)

    def deploy_workload(self) -> None:
        self.workload["container"]["image"] = self.images["mock"]
        (self.workdir / SPEC_NAME).write_text(yaml.safe_dump(self.workload, sort_keys=False))
        result = self.hull("deploy", SPEC_NAME, "--apply", "--wait", "--timeout", "5m")
        if result.returncode != 0:
            raise RuntimeError(f"hull deploy failed:\n{result.stdout}\n{result.stderr}")

    def destroy_workload(self) -> subprocess.CompletedProcess[str]:
        return self.hull("destroy", SPEC_NAME, "--yes")

    def controller_spec(self) -> Path:
        document = workload_spec(self.settings.service, None)
        document["container"]["image"] = self.images["mock"]
        path = self.inputs / SPEC_NAME
        path.write_text(yaml.safe_dump(document, sort_keys=False))
        return path

    def create_inputs(self, credentials: Credentials) -> None:
        namespace = RELEASE_NAMESPACE
        rbac = self.inputs / "rbac.json"
        rbac.write_text(json.dumps(cluster_role_manifest()))
        self.kubectl_ok("apply", "-f", str(rbac))
        claim = self.inputs / "state-claim.json"
        claim.write_text(json.dumps(state_claim_manifest()))
        self.kubectl_ok("apply", "-f", str(claim))
        self.kubectl_ok(
            "create",
            "configmap",
            SPEC_CONFIG_MAP,
            f"--from-file={SPEC_NAME}={self.controller_spec()}",
            namespace=namespace,
        )
        self.kubectl_ok(
            "create",
            "secret",
            "generic",
            STATE_SECRET,
            f"--from-literal=url={STATE_URL}",
            namespace=namespace,
        )
        self.kubectl_ok(
            "create",
            "secret",
            "tls",
            CONTROLLER_TLS_SECRET,
            f"--cert={credentials.server_cert}",
            f"--key={credentials.server_key}",
            namespace=namespace,
        )
        self.kubectl_ok(
            "create",
            "secret",
            "generic",
            CLIENT_CA_SECRET,
            f"--from-file=ca.crt={credentials.ca}",
            namespace=namespace,
        )
        self.kubectl_ok(
            "create",
            "secret",
            "generic",
            TOKEN_SECRET,
            f"--from-file=token={credentials.token}",
            namespace=namespace,
        )
        self.kubectl_ok(
            "create",
            "secret",
            "generic",
            ROUTER_DISCOVERY_SECRET,
            f"--from-file=ca.crt={credentials.ca}",
            f"--from-file=tls.crt={credentials.client_cert}",
            f"--from-file=tls.key={credentials.client_key}",
            namespace=namespace,
        )

    def helm_values(self) -> list[str]:
        controller_repository, controller_tag = split_image(self.images["controller"])
        router_repository, router_tag = split_image(self.images["router"])
        values = {
            "controller.enabled": "true",
            "controller.image.repository": controller_repository,
            "controller.image.tag": controller_tag,
            "controller.stateBackend.existingSecret": STATE_SECRET,
            "controller.stateBackend.volume.persistentVolumeClaim.claimName": STATE_CLAIM,
            "controller.specConfigMap": SPEC_CONFIG_MAP,
            "controller.grpcPort": str(GRPC_PORT),
            "controller.tls.secretName": CONTROLLER_TLS_SECRET,
            "controller.tls.clientCaSecret": CLIENT_CA_SECRET,
            "controller.token.existingSecret": TOKEN_SECRET,
            "router.image.repository": router_repository,
            "router.image.tag": router_tag,
            "router.replicas": "1",
            "router.snapshot.type": "grpc",
            "router.snapshot.tls.secretName": ROUTER_DISCOVERY_SECRET,
            "router.snapshot.token.existingSecret": TOKEN_SECRET,
        }
        return [arg for key, value in values.items() for arg in ("--set", f"{key}={value}")]

    def install(self, credentials: Credentials) -> None:
        self.create_inputs(credentials)
        result = self.helm(
            "install",
            RELEASE,
            str(REPO_ROOT / "charts" / "multihull"),
            "--namespace",
            RELEASE_NAMESPACE,
            "--wait",
            "--timeout",
            "5m",
            *self.helm_values(),
        )
        if result.returncode != 0:
            raise RuntimeError(f"helm install failed:\n{result.stdout}\n{result.stderr}")

    def uninstall(self) -> None:
        self.helm("uninstall", RELEASE, "--namespace", RELEASE_NAMESPACE, "--wait")
        for kind in ("clusterrolebinding", "clusterrole"):
            self.kubectl("delete", kind, CLUSTER_ROLE, "--ignore-not-found")
        self.kubectl("delete", "namespace", RELEASE_NAMESPACE, "--ignore-not-found")

    def pod_logs(self, deployment: str) -> str:
        result = self.kubectl(
            "logs", f"deployment/{deployment}", "--all-containers", namespace=RELEASE_NAMESPACE
        )
        return result.stdout + result.stderr

    def save_pod_logs(self) -> None:
        for deployment in (CONTROLLER, ROUTER):
            (self.logs / f"{deployment}.log").write_text(self.pod_logs(deployment))
        described = self.kubectl("describe", "pods", namespace=RELEASE_NAMESPACE)
        (self.logs / "pods.txt").write_text(described.stdout + described.stderr)

    def restart_controller(self) -> None:
        self.kubectl_ok(
            "rollout", "restart", f"deployment/{CONTROLLER}", namespace=RELEASE_NAMESPACE
        )
        self.kubectl_ok(
            "rollout",
            "status",
            f"deployment/{CONTROLLER}",
            "--timeout=180s",
            namespace=RELEASE_NAMESPACE,
        )

    def exec_controller(self, *command: str) -> subprocess.CompletedProcess[str]:
        return self.kubectl(
            "exec", f"deployment/{CONTROLLER}", "--", *command, namespace=RELEASE_NAMESPACE
        )

    def port_forward_router(self) -> RouterForward:
        forward = RouterForward(self.settings.context, self.logs / "port-forward.log")
        forward.start()
        return forward


class RouterForward(Process):
    def __init__(self, context: str, log_path: Path) -> None:
        super().__init__("port-forward", log_path)
        self.context = context
        self.listen_port = free_port()
        self.admin_port = free_port()
        self.http = httpx.Client(timeout=5.0)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.listen_port}"

    @property
    def admin_url(self) -> str:
        return f"http://127.0.0.1:{self.admin_port}"

    def start(self) -> None:
        self.spawn(
            [
                "kubectl",
                "--context",
                self.context,
                "--namespace",
                RELEASE_NAMESPACE,
                "port-forward",
                f"deployment/{ROUTER}",
                f"{self.listen_port}:{ROUTER_LISTEN}",
                f"{self.admin_port}:{ROUTER_ADMIN}",
            ]
        )

    def healthz(self) -> bool:
        if not self.running:
            raise RuntimeError(f"port-forward exited:\n{self.log_tail()}")
        try:
            return self.http.get(f"{self.admin_url}/healthz").status_code == 200
        except httpx.HTTPError:
            return False

    def debug_endpoints(self) -> dict[str, Any]:
        response = self.http.get(f"{self.admin_url}/debug/endpoints")
        response.raise_for_status()
        return response.json()
