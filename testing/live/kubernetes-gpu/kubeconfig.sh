#!/bin/sh
set -eu

if [ "$#" -lt 2 ]; then
  echo "usage: $0 <admin-context> <output-file> [namespace]" >&2
  exit 2
fi

admin_context=$1
out=$2
namespace=${3:-multihull-gpu}
account=multihull-live
context=multihull-gpu

if [ -e "$out" ]; then
  echo "$out already exists; pass a new path" >&2
  exit 1
fi

admin() {
  kubectl --context "$admin_context" "$@"
}

server=$(admin config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')
ca=$(admin --namespace "$namespace" get secret "$account-token" -o jsonpath='{.data.ca\.crt}')
token=$(admin --namespace "$namespace" get secret "$account-token" -o jsonpath='{.data.token}' | base64 --decode)

if [ -z "$server" ] || [ -z "$ca" ] || [ -z "$token" ]; then
  echo "secret $namespace/$account-token is not populated yet; apply rbac.yaml, wait a few seconds, retry" >&2
  exit 1
fi

umask 077
cat >"$out" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: $context
    cluster:
      server: $server
      certificate-authority-data: $ca
users:
  - name: $account
    user:
      token: $token
contexts:
  - name: $context
    context:
      cluster: $context
      user: $account
      namespace: $namespace
current-context: $context
EOF

if KUBECONFIG=$out kubectl auth can-i create deployments.apps --namespace "$namespace" >/dev/null; then
  echo "wrote $out: service account $namespace/$account can deploy to $namespace"
else
  echo "wrote $out, but service account $namespace/$account cannot create deployments in $namespace" >&2
  exit 1
fi
