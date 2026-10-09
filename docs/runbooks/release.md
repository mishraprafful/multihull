# Runbook: cutting a release

A `v<semver>` tag on `main` drives everything. `release.yml` builds the Python wheel and router binaries, creates the GitHub release, then publishes to PyPI, crates.io and GHCR. `images.yml` tags the three images with the version. Nothing is published by hand.

## Prerequisites (once)

- Versions on `main` match the tag: `python/pyproject.toml`, `router/Cargo.toml` (workspace version) and `charts/multihull/Chart.yaml` (`version` and `appVersion`). `charts/package.sh` fails the chart job otherwise.
- GitHub environment `release` exists. `release.yml` and both trusted publishers reference it; add required reviewers there if a human gate is wanted.
- Repository variable `PUBLISH_ENABLED` is `true`. Without it the three publish jobs are skipped and the tag produces only the GitHub release.
- Trusted publishers configured for `release.yml` with environment `release`: PyPI project `multihull` and crates.io crate `multihull`. Owner-only check; the public APIs cannot show it.
- `CHANGELOG.md` has the version's section, `docs/releases/<version>.md` exists and the website sidebar points at it (`website/scripts/sync-design-docs.mjs`).

## Steps

1. Merge every PR in the milestone. Confirm `main` is green: CI, `kind.yml` (both jobs) and the latest `live-smoke.yml` run.
2. Date the changelog: rename `## [Unreleased]` to `## [<version>] - <YYYY-MM-DD>` and move the compare link. Merge that PR.
3. Tag from a clean `main`:

   ```sh
   git fetch origin && git checkout main && git pull --ff-only
   git tag -a v0.1.0 -m "v0.1.0" && git push origin v0.1.0
   ```

4. Watch the runs:

   ```sh
   gh run watch "$(gh run list --workflow release.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
   gh run list --workflow images.yml --limit 3
   ```

   `publish-chart` waits up to 30 minutes for `multihull-router:<version>` and `multihull-controller:<version>` from `images.yml` before it pushes.

## Verify

```sh
pip install multihull==0.1.0 && hull --help
cargo install multihull --version 0.1.0 && multihull --help
helm pull oci://ghcr.io/mishraprafful/charts/multihull --version 0.1.0
gh release view v0.1.0
```

- The GitHub release lists `multihull-linux-amd64`, `multihull-linux-arm64`, `multihull-darwin-arm64` and the wheel and sdist.
- GHCR packages `multihull-router`, `multihull-controller`, `multihull-mock-server` and `charts/multihull` carry the version tag, are linked to the repository and stay private until the owner switches them to public.
- `helm show values oci://ghcr.io/mishraprafful/charts/multihull --version 0.1.0` prints the chart's values.

## After

- `docs/handover.md`: current state and next steps.
- Quickstart and install snippets name the released version.
- Close the milestone. The tag closes the release issue.

## When a publish job fails

Fix forward. PyPI and crates.io never reuse a version, so a failed `v0.1.0` becomes `v0.1.1`: bump the three version files, land the fix, tag again. A failed `publish-chart` alone can be re-run from the Actions UI once the images exist.

### If `publish-pypi` fails with `invalid-pending-publisher`

Seen on 0.1.0 (run 37895991523): PyPI answered `valid token, but project already exists` although the token claims matched the project publisher. The build artifacts and the other publish jobs stay valid, so rerun only the failed job first; the 0.1.0 rerun passed about 25 minutes later and the cause was not identified.

```sh
gh run rerun <run id> --failed
```

If it fails again, check two PyPI pages as the owner: the project's publishing settings (`https://pypi.org/manage/project/multihull/settings/publishing/`) and the account's pending publishers (`https://pypi.org/manage/account/publishing/`). PyPI checks pending publishers before project publishers, so a pending entry for `multihull` blocks uploads to the existing project; remove it and keep only the project publisher.

Never delete or move a published tag.
