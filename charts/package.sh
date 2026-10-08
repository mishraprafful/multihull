#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <v-prefixed release tag> <destination dir>" >&2
  exit 2
fi

tag=$1
destination=$2
identifier='(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)'
semver="^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-$identifier(\.$identifier)*)?$"

if [[ $tag != v* || ! ${tag#v} =~ $semver ]]; then
  echo "tag '$tag' is not v<semver> without build metadata" >&2
  exit 1
fi

version=${tag#v}
chart="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/multihull"
helm="${HELM:-helm}"
package="$destination/multihull-$version.tgz"

check_image() {
  local component=$1 expected=$2 image
  shift 2
  image="$("$helm" template multihull "$package" --show-only "templates/$component-deployment.yaml" "$@" | sed -n 's/^ *image: "\(.*\)"$/\1/p')"
  if [[ $image != "$expected" ]]; then
    echo "$component image is '$image', expected '$expected'" >&2
    exit 1
  fi
}

"$helm" package "$chart" --version "$version" --app-version "$version" --destination "$destination"
"$helm" lint "$package"

check_image router "ghcr.io/mishraprafful/multihull-router:$version"
check_image controller "ghcr.io/mishraprafful/multihull-controller:$version" \
  --set controller.enabled=true --set controller.stateBackend.existingSecret=state \
  --set controller.insecure=true
