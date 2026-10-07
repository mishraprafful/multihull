# Runbook: CI runner images

Every workflow pins `runs-on: ubuntu-24.04` (and `ubuntu-24.04-arm` for the arm64 release build). We never use `ubuntu-latest`, so the image changes when we choose, not when GitHub moves the label ([runner-images#14748](https://github.com/actions/runner-images/issues/14748): `ubuntu-latest` moves to Ubuntu 26.04 between October 19 and November 19, 2026). `macos-latest` in `release.yml` already points to macOS 26 and has no pending migration.

`runner-canary.yml` runs the Python tests, Rust tests and docs build on `ubuntu-26.04` every Monday and on demand. It is not a PR check, so a red run blocks nothing. kind, e2e and live jobs are not part of it.

Known 24.04 to 26.04 differences that touch us: Python 3.12 to 3.14, Node 22 to 24, Docker 28 to 29, Helm 3 to 4 (we install Helm with `azure/setup-helm`), kernel 6.17 to 7.0.

## Moving to the next image

1. Run the canary: `gh workflow run runner-canary.yml`. Fix anything red.
2. Replace `ubuntu-24.04` with the new label in every workflow (`grep -rn 'ubuntu-24.04' .github/workflows`), in one PR, and watch kind and e2e on it.
3. Point the canary at the following release once one is available.

runner-images supports at most two GA images per OS and deprecates the oldest once a newer one goes GA, so 24.04 stays until the next Ubuntu LTS image ships. Move before then; deprecations are announced under the [Announcement](https://github.com/actions/runner-images/labels/Announcement) label.
