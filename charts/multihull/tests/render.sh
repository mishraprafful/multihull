#!/usr/bin/env bash
set -euo pipefail

chart="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
values="$chart/tests/values"
helm="${HELM:-helm}"
multihull="${MULTIHULL_BIN:-$chart/../../router/target/release/multihull}"
read -r -a hull <<<"${HULL:-hull}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
failures=0

fail() {
  echo "FAIL $1: $2"
  failures=$((failures + 1))
}

render() {
  local name=$1
  shift
  "$helm" template multihull "$chart" --show-only templates/configmap.yaml "$@" >"$work/$name.yaml" || return 1
  sed -n '/^  router.toml: |$/,$p' "$work/$name.yaml" | tail -n +2 | sed 's/^    //' >"$work/$name.toml"
}

accepted() {
  local name=$1
  shift
  if ! render "$name" "$@"; then
    fail "$name" "helm template failed"
    return 0
  fi
  if ! "$multihull" --config "$work/$name.toml" --check-config >/dev/null 2>"$work/$name.err"; then
    fail "$name" "router rejected the rendered config: $(cat "$work/$name.err")"
    sed 's/^/    /' "$work/$name.toml"
    return 0
  fi
  echo "ok   $name"
}

rejected() {
  local name=$1 reason=$2
  shift 2
  if ! render "$name" "$@"; then
    fail "$name" "helm template failed"
    return 0
  fi
  if "$multihull" --config "$work/$name.toml" --check-config >/dev/null 2>"$work/$name.err"; then
    fail "$name" "router accepted a config it should reject"
  elif ! grep -qF "$reason" "$work/$name.err"; then
    fail "$name" "expected '$reason' in: $(cat "$work/$name.err")"
  else
    echo "ok   $name (rejected: $reason)"
  fi
}

has_line() {
  local name=$1 line=$2
  grep -qxF "$line" "$work/$name.toml" || fail "$name" "missing line '$line'"
}

before() {
  local name=$1 first=$2 second=$3 a b
  a=$(grep -nxF "$first" "$work/$name.toml" | head -1 | cut -d: -f1)
  b=$(grep -nxF "$second" "$work/$name.toml" | head -1 | cut -d: -f1)
  if [ -z "$a" ] || [ -z "$b" ] || [ "$a" -ge "$b" ]; then
    fail "$name" "expected '$first' before '$second'"
  fi
}

template_fails() {
  local name=$1 reason=$2
  shift 2
  if "$helm" template multihull "$chart" "$@" >/dev/null 2>"$work/$name.err"; then
    fail "$name" "helm template succeeded but should fail"
  elif ! grep -qF "$reason" "$work/$name.err"; then
    fail "$name" "expected '$reason' in: $(cat "$work/$name.err")"
  else
    echo "ok   $name (template failed: $reason)"
  fi
}

controller_accepted() {
  local name=$1 line
  shift
  if ! "$helm" template multihull "$chart" --show-only templates/controller-deployment.yaml "$@" >"$work/$name-controller.yaml"; then
    fail "$name" "helm template failed"
    return 0
  fi
  awk '/^ *command:$/ {list = 1; next} list && /^ *- / {sub(/^ *- /, ""); gsub(/"/, ""); print; next} list {exit}' \
    "$work/$name-controller.yaml" >"$work/$name.args"
  local args=()
  while IFS= read -r line; do
    args+=("$line")
  done <"$work/$name.args"
  if [ "${#args[@]}" -lt 3 ] || [ "${args[0]}" != hull ] || [ "${args[1]}" != controller ]; then
    fail "$name" "controller command is not 'hull controller ...': ${args[*]}"
  elif [ "${args[2]}" != /etc/multihull/multihull.yaml ]; then
    fail "$name" "controller spec argument is '${args[2]}', expected /etc/multihull/multihull.yaml"
  elif ! "${hull[@]}" "${args[@]:1}" --help >/dev/null 2>"$work/$name.err"; then
    fail "$name" "hull rejected the controller args '${args[*]}': $(cat "$work/$name.err")"
  else
    echo "ok   $name (hull controller accepts: ${args[*]:2})"
  fi
}

manifest_has() {
  local name=$1 template=$2 text=$3
  shift 3
  if ! "$helm" template multihull "$chart" --show-only "templates/$template" "$@" >"$work/$name-manifest.yaml"; then
    fail "$name" "helm template failed for $template"
  elif ! grep -qF -- "$text" "$work/$name-manifest.yaml"; then
    fail "$name" "missing '$text' in $template"
  fi
}

manifest_lacks() {
  local name=$1 template=$2 text=$3
  shift 3
  if ! "$helm" template multihull "$chart" --show-only "templates/$template" "$@" >"$work/$name-manifest.yaml"; then
    fail "$name" "helm template failed for $template"
  elif grep -qF -- "$text" "$work/$name-manifest.yaml"; then
    fail "$name" "unexpected '$text' in $template"
  fi
}

rendered_lacks() {
  local name=$1 text=$2
  shift 2
  if ! "$helm" template multihull "$chart" "$@" >"$work/$name-all.yaml"; then
    fail "$name" "helm template failed"
  elif grep -qF -- "$text" "$work/$name-all.yaml"; then
    fail "$name" "unexpected '$text' in the full render"
  fi
}

lacks() {
  local name=$1 text=$2
  if grep -qF "$text" "$work/$name.toml"; then
    fail "$name" "unexpected '$text'"
  fi
}

accepted defaults
has_line defaults "bound = 1024"
has_line defaults "ttft_degrade_factor = 2"

controller=(
  --set controller.enabled=true --set controller.stateBackend.existingSecret=state
  --set controller.specConfigMap=spec
)
controller_tls=(
  "${controller[@]}" --set controller.tls.secretName=controller-tls
  --set controller.tls.clientCaSecret=discovery-ca
  --set controller.token.existingSecret=discovery-token
)
router_tls=(
  --set router.snapshot.type=grpc --set router.snapshot.tls.secretName=router-discovery
  --set router.snapshot.token.existingSecret=discovery-token
)

accepted grpc-tls-region "${controller_tls[@]}" "${router_tls[@]}" \
  --set router.tls.secretName=router-tls --set router.region=eu
has_line grpc-tls-region 'region = "eu"'
has_line grpc-tls-region "[tls]"

accepted large-integers -f "$values/large-integers.yaml"
has_line large-integers "bound = 1000000"
has_line large-integers "min_samples = 10000000"
has_line large-integers "min_retries_per_second = 123456789"
lacks large-integers "e+"

accepted all-tuning -f "$values/all-tuning.yaml"
for table in timeouts circuit admission pressure probe retry; do
  has_line all-tuning "[$table]"
done
has_line all-tuning "connect = 1.5"
has_line all-tuning "ttft_degrade_factor = 3.5"
has_line all-tuning "enabled = false"

accepted set-flags \
  --set router.tuning.admission.bound=1000000 \
  --set router.tuning.circuit.error_ratio=0.25 \
  --set router.tuning.timeouts.connect=1.5 \
  --set-string router.tuning.probe.interval=7
has_line set-flags "bound = 1000000"
has_line set-flags "error_ratio = 0.25"
has_line set-flags "connect = 1.5"
has_line set-flags "interval = 7"

accepted null-tuning -f "$values/null-tuning.yaml"
lacks null-tuning "[timeouts]"

rejected string-duration 'invalid type: string "2s"' --set router.tuning.timeouts.connect=2s
has_line string-duration 'connect = "2s"'

accepted extra-config -f "$values/extra-config.yaml"
before extra-config 'upstream_ca = "/etc/multihull/upstream-ca.pem"' "[snapshot]"
before extra-config "max_buffered_body_bytes = 4194304" "[snapshot]"
before extra-config "[retry]" "[log]"

accepted extra-config-tables-only -f "$values/extra-config-tables-only.yaml"
before extra-config-tables-only "[retry]" "[log]"

template_fails extra-config-map "router.extraConfig must be a string of TOML" \
  --set router.extraConfig.node_id=router-eu

controller_accepted controller-args "${controller_tls[@]}"

state_volume=(--set controller.stateBackend.volume.persistentVolumeClaim.claimName=multihull-state)
manifest_has controller-state-volume controller-deployment.yaml "mountPath: /var/lib/multihull/state" \
  "${controller_tls[@]}" "${state_volume[@]}"
manifest_has controller-state-volume controller-deployment.yaml "claimName: multihull-state" \
  "${controller_tls[@]}" "${state_volume[@]}"
manifest_has controller-state-volume controller-deployment.yaml "fsGroup: 10001" \
  "${controller_tls[@]}" "${state_volume[@]}"
manifest_lacks controller-no-state-volume controller-deployment.yaml "/var/lib/multihull/state" \
  "${controller_tls[@]}"
controller_accepted controller-state-volume-args "${controller_tls[@]}" "${state_volume[@]}"
manifest_has controller-env-from controller-deployment.yaml "name: modal-credentials" \
  "${controller_tls[@]}" --set 'controller.envFrom[0].secretRef.name=modal-credentials'
manifest_lacks controller-no-env-from controller-deployment.yaml "envFrom" "${controller_tls[@]}"

accepted grpc-mtls-token "${controller_tls[@]}" "${router_tls[@]}"
has_line grpc-mtls-token 'source = "grpcs://multihull-controller:9443"'
has_line grpc-mtls-token 'ca = "/etc/multihull/discovery/ca.crt"'
has_line grpc-mtls-token 'client_cert = "/etc/multihull/discovery/tls.crt"'
has_line grpc-mtls-token 'client_key = "/etc/multihull/discovery/tls.key"'
has_line grpc-mtls-token 'token_env = "MULTIHULL_DISCOVERY_TOKEN"'
lacks grpc-mtls-token "insecure"
manifest_has grpc-mtls-token router-deployment.yaml "secretName: router-discovery" \
  "${controller_tls[@]}" "${router_tls[@]}"
manifest_has grpc-mtls-token router-deployment.yaml "name: MULTIHULL_DISCOVERY_TOKEN" \
  "${controller_tls[@]}" "${router_tls[@]}"
manifest_has grpc-mtls-token controller-deployment.yaml "secretName: discovery-ca" \
  "${controller_tls[@]}"
manifest_has grpc-mtls-token controller-deployment.yaml "name: discovery-token" \
  "${controller_tls[@]}"
controller_accepted grpc-mtls-token-controller "${controller_tls[@]}"

accepted grpc-tls-token-only "${controller[@]}" \
  --set controller.tls.secretName=controller-tls \
  --set controller.token.existingSecret=discovery-token \
  --set router.snapshot.type=grpc --set router.snapshot.tls.secretName=router-discovery \
  --set router.snapshot.tls.clientCertificate=false \
  --set router.snapshot.token.existingSecret=discovery-token
has_line grpc-tls-token-only 'ca = "/etc/multihull/discovery/ca.crt"'
lacks grpc-tls-token-only "client_cert"
controller_accepted grpc-tls-token-only-controller "${controller[@]}" \
  --set controller.tls.secretName=controller-tls \
  --set controller.token.existingSecret=discovery-token

accepted grpc-external-mtls --set router.snapshot.type=grpc \
  --set router.snapshot.value=controller.example.com:7700 \
  --set router.snapshot.tls.secretName=router-discovery
has_line grpc-external-mtls 'source = "grpcs://controller.example.com:7700"'
lacks grpc-external-mtls "token_env"

accepted grpc-plaintext-opt-in "${controller[@]}" --set controller.insecure=true \
  --set router.snapshot.type=grpc --set router.snapshot.insecure=true
has_line grpc-plaintext-opt-in 'source = "grpc://multihull-controller:9443"'
has_line grpc-plaintext-opt-in "insecure = true"
manifest_has grpc-plaintext-opt-in controller-deployment.yaml '- "--insecure"' \
  "${controller[@]}" --set controller.insecure=true
controller_accepted grpc-plaintext-opt-in-controller "${controller[@]}" \
  --set controller.insecure=true

accepted https-token --set router.snapshot.type=http \
  --set router.snapshot.value=https://snapshots.example.com/llama.json \
  --set router.snapshot.token.existingSecret=snapshot-token
has_line https-token 'source = "https://snapshots.example.com/llama.json"'
has_line https-token 'token_env = "MULTIHULL_DISCOVERY_TOKEN"'
lacks https-token "ca = "

accepted http-plaintext-opt-in --set router.snapshot.type=http \
  --set router.snapshot.value=http://snapshots.local/llama.json --set router.snapshot.insecure=true
has_line http-plaintext-opt-in "insecure = true"

manifest_has workload-namespace namespace.yaml 'name: "multihull"'
manifest_has workload-namespace namespace.yaml "multihull.dev/managed-by: multihull"
manifest_has workload-namespace namespace.yaml "helm.sh/resource-policy: keep"
manifest_has workload-namespace-custom namespace.yaml 'name: "inference"' \
  --set workloads.namespace=inference
rendered_lacks workload-namespace-is-release "kind: Namespace" --namespace multihull
manifest_has workload-namespace-forced namespace.yaml 'name: "multihull"' \
  --namespace multihull --set workloads.forceCreateNamespace=true
rendered_lacks workload-namespace-disabled "kind: Namespace" --set workloads.createNamespace=false

template_fails grpc-without-tls "router.snapshot.tls.secretName" \
  "${controller_tls[@]}" --set router.snapshot.type=grpc
template_fails http-plaintext-without-opt-in "is plaintext" \
  --set router.snapshot.type=http --set router.snapshot.value=http://snapshots.local/llama.json
template_fails grpc-insecure-with-tls "cannot be combined" \
  "${controller_tls[@]}" "${router_tls[@]}" --set router.snapshot.insecure=true
template_fails https-insecure "only permits plaintext" --set router.snapshot.type=http \
  --set router.snapshot.value=https://snapshots.example.com/llama.json \
  --set router.snapshot.insecure=true
template_fails file-with-token "not to a snapshot file" \
  --set router.snapshot.token.existingSecret=discovery-token
template_fails grpc-without-address "host:port" --set router.snapshot.type=grpc \
  --set router.snapshot.tls.secretName=router-discovery
template_fails controller-without-tls "controller.tls.secretName is required" "${controller[@]}"
template_fails controller-tls-without-client-auth "controller.tls.clientCaSecret" \
  "${controller[@]}" --set controller.tls.secretName=controller-tls
template_fails controller-insecure-with-tls "controller.insecure cannot be combined" \
  "${controller_tls[@]}" --set controller.insecure=true

if [ "$failures" -gt 0 ]; then
  echo "$failures chart render check(s) failed"
  exit 1
fi
echo "all chart render checks passed"
