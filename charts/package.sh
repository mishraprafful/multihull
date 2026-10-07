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
expected_image="ghcr.io/mishraprafful/multihull-router:$version"

"$helm" package "$chart" --version "$version" --app-version "$version" --destination "$destination"
"$helm" lint "$package"

image="$("$helm" template multihull "$package" --show-only templates/router-deployment.yaml | sed -n 's/^ *image: "\(.*\)"$/\1/p')"
if [[ $image != "$expected_image" ]]; then
  echo "router image is '$image', expected '$expected_image'" >&2
  exit 1
fi
