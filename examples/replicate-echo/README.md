# Replicate echo

A Cog model that returns its `text` input, served by a local Docker container (primary) and a Replicate deployment (secondary). Both run the same Cog image and answer `POST /predictions`, so the router fails over between them without translating requests.

```sh
cd examples/replicate-echo
cog build -t replicate-echo:build   # the image the docker target runs
hull doctor
hull deploy --apply --wait
```

`.dockerignore` keeps `.multihull/` (state, plans, the snapshot with the API token) and `multihull.yaml` out of the image; without it `hull deploy` refuses to push from this directory. `hull deploy` creates the private model `<account>/multihull-replicate-echo` if missing, runs `cog push r8.im/<account>/multihull-replicate-echo` here, then creates or updates the deployment `multihull-replicate-echo-replicate` with `min_instances: 1`.

Credentials: `REPLICATE_API_TOKEN` for the API and the router; `REPLICATE_CLI_AUTH_TOKEN` (from replicate.com/auth/token) for `cog login`, or log in once with `cog login` yourself.

The router forwards only `POST /predictions` to Replicate. Call it with a Cog prediction body:

```sh
curl -s -H 'Host: echo.local' -H 'Content-Type: application/json' \
  -d '{"input": {"text": "hello"}}' http://127.0.0.1:8080/predictions
```

`hull destroy` scales the deployment to zero and deletes it once Replicate allows (after 15 minutes offline and unused); run it again after that.
