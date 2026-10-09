# 2026-10-10 Launch checklist assets (issue 141)

- Social preview: `website/public/social-preview.png`, 1280x640, 22 KB, rendered from the mark and the Open water palette with a throwaway Pillow script; uploaded by hand under Settings, General.
- Cloud demo run `live-demo-305a64`: kind plus Modal, 180 s scripted, 511 requests, 0 errors, 139 failovers, Modal bill 0.0012 USD; recorded in `docs/runbooks/demo-cloud.md`. Host port 30081 stood in for 30080 because another kind cluster held it.
- `ghcr.io/mishraprafful/multihull-mock-server:main` is `linux/amd64` only (`images.yml`), so the arm64 kind node could not pull it (first attempt `live-demo-60895a`, ImagePullBackOff, torn down clean). Building the image locally under the same tag makes the demo `kind load` it while Modal keeps pulling from GHCR.
- CodeQL alerts 2 and 4 were dismissed by the owner as false positives.
- `v0.1.0` keeps `isPrerelease: true`; `release.yml` never sets the flag, so it was set by hand.
