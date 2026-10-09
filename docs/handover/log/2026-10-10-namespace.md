# 2026-10-10 Namespace owned by the chart

Issue 148. `hull init` wrote `namespace: inference`, a fresh cluster had none, and `hull deploy` printed the Kubernetes client's `ApiException` dump in the table.

Decision: the namespace is `multihull`, the chart creates it, `hull` never does.

Lessons
- Helm cannot create its release namespace from a template, so `templates/namespace.yaml` is skipped when `workloads.namespace` equals `.Release.Namespace`; the install uses `--create-namespace`.
- Helm refuses to adopt an existing resource without its ownership metadata. The in-cluster suite creates the workload namespace by hand before the chart installs (the controller must rediscover an existing workload), so it passes `workloads.createNamespace=false`.
- The Namespace carries `helm.sh/resource-policy: keep`: `helm uninstall` must not delete the workloads `hull` deployed into it.
