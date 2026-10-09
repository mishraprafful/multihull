#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <artifact hub repository id> <destination dir>" >&2
  exit 2
fi

repository_id=$1
destination=$2
source="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/artifacthub-repo.yml"
rendered="$destination/artifacthub-repo.yml"
yq="${YQ:-yq}"

if [[ ! $repository_id =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
  echo "repository id '$repository_id' is not a UUID" >&2
  exit 1
fi

mkdir -p "$destination"
sed "s/^repositoryID: ARTIFACTHUB_REPOSITORY_ID\$/repositoryID: $repository_id/" "$source" >"$rendered"

if ! "$yq" -e ".repositoryID == \"$repository_id\"" "$rendered" >/dev/null; then
  echo "repositoryID was not rendered into $rendered" >&2
  exit 1
fi
if ! "$yq" -e '(.owners | length > 0) and (.owners | all_c(has("name") and (.name | tag == "!!str") and (.name | length > 0)))' "$rendered" >/dev/null; then
  echo "owners must list at least one entry with a name" >&2
  exit 1
fi
if ! "$yq" -e 'keys | all_c(. == "repositoryID" or . == "owners" or . == "ignore")' "$rendered" >/dev/null; then
  echo "unexpected top-level key in $rendered" >&2
  exit 1
fi

echo "$rendered"
