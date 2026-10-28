# PDF Accessibility Worker

`cail-pdf-accessibility`: version 2 of PDF Accessibility behind CAIL Doorway.
It serves the React app (`../frontend`) at `/pdf-accessibility`, answers the
app's API for the signed-in person, keeps jobs in the `JobBoard` Durable
Object and files in R2, gives work to the runner, and relays the runner's
model calls to the Gateway with the person's credential. The design is in
[../docs/architecture.md](../docs/architecture.md#version-2).

## Commands

Bun installs and runs scripts; tests and `wrangler dev` run under Node 22.18
or later.

```bash
bun install
bun run check        # lint, types, build (frontend at /pdf-accessibility/), tests
bun run test         # the Worker in workerd, with a Gateway double
```

`dev/e2e.mjs` runs this Worker (`wrangler dev`) and the real runner on one
PDF. Identity legs are signed by a throwaway local key, and the
dev-only `dev/gateway.ts` sends model calls to the real Gateway with your
own key:

```bash
DEV_GATEWAY_KEY="$(security find-generic-password -s cail-gateway -w)" \
DOCLING_SERVE_URL=https://actual-dell.taila37893.ts.net:8444 \
node dev/e2e.mjs path/to/file.pdf
```

## Configuration

- Bindings: `GATEWAY` (service `cail-model-api`), `FILES` (R2
  `cail-pdf-accessibility-files`), `JOB_BOARD` (Durable Object), `ASSETS`.
- Secrets: `CAIL_IDENTITY_JWKS` (Doorway's public JWKS, from
  `https://tools.ailab.gc.cuny.edu/cail-sso/.well-known/jwks.json`) and
  `RUNNER_TOKEN` (shared with the runner).
- The R2 bucket needs a lifecycle rule expiring objects after two days; the
  release refuses to ship without it.

## Release

`.github/workflows/worker-release.yml`, run by hand on `main` with
`deploy: true`. It checks the Worker, reads back the bucket rule, uploads the
exact commit, reads back its bindings, promotes it to 100 percent, confirms,
and rolls back on failure. The first release uses `wrangler deploy`, which
creates the Durable Object. Deploy this Worker before the Doorway release
that binds it.
