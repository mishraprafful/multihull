#!/usr/bin/env bash
set -euo pipefail

chart="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
values="$chart/tests/values"
helm="${HELM:-helm}"
multihull="${MULTIHULL_BIN:-$chart/../../router/target/release/multihull}"
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

if [ "$failures" -gt 0 ]; then
  echo "$failures chart render check(s) failed"
  exit 1
fi
echo "all chart render checks passed"
