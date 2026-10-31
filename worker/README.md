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

With `E2E_BROWSER=1` it uploads through the app in Chromium
(`dev/ui_check.py`, via the backend's Playwright) and follows the job page to
its download link.

## Outcomes

When a process or review task ends, the JobBoard logs one `job_finished`
event to Workers Observability (`src/outcome.ts`): the final status
(`complete`, `manual_remediation`, `failed`) and why a job failed, its
classification and pages, each step's seconds, the review tasks by type and
how many block, blocking tasks the review screen does not show, the release
gate checks that failed or could not run, and the model use the steps
report. It takes these from the runner's job view and the review tasks and
report it uploads, and holds no file name, person, error message, or text
from the document. `dev/outcomes.ts` reads the events for a window (up to
the seven days Workers Logs keep) and prints the totals:

```bash
bun dev/outcomes.ts --days 7 --price-in 0.53 --price-out 2.66
```

It needs `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`. The Gateway
reports no cost per call, so the cost comes from the tokens at the prices
given (the model's catalog price per million tokens).

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
creates the Worker and its Durable Object; set the two secrets after it
(`wrangler secret put`) and release again, which reads them back. Deploy
this Worker before the Doorway release that binds it.
