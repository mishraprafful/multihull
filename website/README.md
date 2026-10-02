Multihull docs site (Astro Starlight, "Open water" theme).

Run: `npm install && npm run dev` (http://localhost:4321). Build: `npm run build` (output in `dist/`).

`npm run build` first generates three pages; edit the sources, not the copies:

- `docs/design/architecture.md` from `../docs/design/architecture-plan.md`
- `docs/reference/spec-schema.mdx` from `uv run --project ../python hull schema`
- `docs/reference/cli.mdx` from `uv run --project ../python hull --help`

The reference pages need `uv` on `PATH`; the build fails with a clear message if it is missing.

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
