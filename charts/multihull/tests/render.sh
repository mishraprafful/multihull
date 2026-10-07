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

lacks() {
  local name=$1 text=$2
  if grep -qF "$text" "$work/$name.toml"; then
    fail "$name" "unexpected '$text'"
  fi
}

accepted defaults
has_line defaults "bound = 1024"
has_line defaults "ttft_degrade_factor = 2"

accepted grpc-tls-region \
  --set controller.enabled=true --set controller.stateBackend.existingSecret=state \
  --set controller.specConfigMap=spec --set router.snapshot.type=grpc \
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

controller_accepted controller-args \
  --set controller.enabled=true --set controller.stateBackend.existingSecret=state \
  --set controller.specConfigMap=spec

if [ "$failures" -gt 0 ]; then
  echo "$failures chart render check(s) failed"
  exit 1
fi
echo "all chart render checks passed"
