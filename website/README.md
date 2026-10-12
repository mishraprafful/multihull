Multihull docs site (Astro Starlight, "Open water" theme).

Run: `npm install && npm run dev` (http://localhost:4321). Build: `npm run build` (output in `dist/`).

`npm run build` first generates five pages; edit the sources, not the copies:

- `docs/design/architecture.md` from `../docs/design/architecture-plan.md`
- `docs/reference/release-notes.md` from `../docs/releases/0.2.0.md`
- `docs/reference/release-notes/0.1.0.md` from `../docs/releases/0.1.0.md`
- `docs/reference/spec-schema.mdx` from `uv run --project ../python hull schema`
- `docs/reference/cli.mdx` from `uv run --project ../python hull --help`

The reference pages need `uv` on `PATH`; the build fails with a clear message if it is missing.

## Docs versions

The header has a version dropdown: `latest` (whatever `main` holds) plus every released version. The `starlight-versions` plugin serves each release from a snapshot committed in the repo: `src/content/docs/<version>/` holds the pages (generated pages included, rewritten to `/<version>/...` links) and `src/content/versions/<version>.json` the sidebar at that release. Pages of an old version carry a notice linking to the same path on latest.

To add a version, from a checkout whose docs match the release:

```sh
# astro.config.mjs: add { slug: '<version>' } at the front of starlightVersions versions
cd website && npm run build
git add astro.config.mjs src/content/docs/<version> src/content/versions/<version>.json
```

The build snapshots `src/content/docs/` into the new directory the first time it sees a slug without one, and leaves existing snapshots alone. Snapshots are plain content, so every build (production, PR previews, CI) renders all versions; one version adds about 20 pages.

## Deploy

`.github/workflows/docs.yml` builds the site in GitHub Actions and deploys `dist/` to the Cloudflare Pages project `multihull` with Wrangler, creating the project on first run. Pushes to `main` deploy to production (`https://multihull.pages.dev`). Pull requests get a preview deployment and a sticky comment with its URL. Without the secrets below, the workflow builds and skips the deploy.

Add the two repository secrets from a terminal, so values never land in chat or files:

```sh
gh secret set CLOUDFLARE_API_TOKEN      # prompts, value not echoed
gh secret set CLOUDFLARE_ACCOUNT_ID
```

Cloudflare keeps preview deployments forever, so `.github/workflows/docs-preview-sweep.yml` runs daily and deletes every preview deployment older than 24 hours. Production deployments are never touched. A preview link in a PR comment therefore works for about a day; push again to get a fresh one. To sweep by hand, or to preview what a sweep would delete:

```sh
gh workflow run docs-preview-sweep.yml -f older_than_hours=24 -f dry_run=true
```

The API token needs the account permission "Cloudflare Pages: Edit". Cloudflare's Git integration is not used, because the build needs `uv` and Python.

Custom domain: attach it to the `multihull` project in the Cloudflare dashboard, then set `SITE_URL` to it in the workflow's `env`. Without `SITE_URL`, `astro.config.mjs` uses `https://multihull.pages.dev`.
