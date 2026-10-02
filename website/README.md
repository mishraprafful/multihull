Multihull docs site (Astro Starlight, "Open water" theme).

Run: `npm install && npm run dev` (http://localhost:4321). Build: `npm run build` (output in `dist/`).

`npm run build` first generates three pages; edit the sources, not the copies:

- `docs/design/architecture.md` from `../docs/design/architecture-plan.md`
- `docs/reference/spec-schema.mdx` from `uv run --project ../python hull schema`
- `docs/reference/cli.mdx` from `uv run --project ../python hull --help`

The reference pages need `uv` on `PATH`; the build fails with a clear message if it is missing.
