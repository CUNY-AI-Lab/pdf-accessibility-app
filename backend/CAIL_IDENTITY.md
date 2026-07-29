# CAIL identity boundary

## Status

The FastAPI source can verify the short-lived identity assertion issued by the
CAIL Tools proxy. This is source-ready only. The app has not been rebuilt,
deployed, configured with the production public signing keys, or switched to
identity-required mode.

The app does not implement CUNYLogin. The Tools proxy owns CUNY authorization,
the registered callback, OIDC state and nonce validation, session and logout,
group normalization, and issuance of the app-specific assertion.

## Assertion contract

Protected PDF Accessibility routes accept one
`X-CAIL-Identity-JWT` assertion with:

- issuer `https://tools.ailab.gc.cuny.edu/cail-sso`;
- exact scalar audience `cail:pdf-accessibility`;
- algorithm `RS256` and a reviewed public key selected by `kid`;
- canonical stable subject `cail-` followed by 32 lowercase hexadecimal
  characters;
- a valid expiration and, when present, not-before time;
- an optional canonical operational subject in `log_sub`.

The verifier rejects malformed tokens, wrong signatures, algorithms, key IDs,
issuers, audiences, time bounds, and subjects. Configuration failures return a
generic retryable 503. Missing or invalid assertions return a generic
non-retryable 401. The health endpoints remain public for orchestration.

Email, names, raw CUNY claims, groups, access tokens, and ID tokens are not
accepted as app identity. The app uses a domain-separated SHA-256 hash of the
verified stable CAIL subject for job ownership. The existing random browser
cookie remains separate and continues to protect requests with CSRF checks.

## Activation inputs

The existing Compose `env_file` passes these settings to the backend:

```env
CAIL_IDENTITY_REQUIRED=true
CAIL_IDENTITY_JWKS={"keys":[<reviewed public RSA JWKs>]}
CAIL_IDENTITY_ISSUER=https://tools.ailab.gc.cuny.edu/cail-sso
CAIL_IDENTITY_CLOCK_TOLERANCE_SECONDS=60
ANONYMOUS_SESSION_COOKIE_SECURE=true
```

`CAIL_IDENTITY_JWKS` contains public verification material, not the CUNY client
secret or the proxy's private signing key. The audience is fixed in source so
an environment typo cannot make the app accept an assertion for another tool.

Activation still requires the accepted production JWKS, a rebuilt and deployed
app revision, closure of any direct-origin bypass, a proxy canary, and explicit
production approval. Set `CAIL_IDENTITY_REQUIRED=false` or roll back the app
revision only as part of the reviewed proxy rollback; disabling the app
verifier while the route remains exposed would reopen the bypass.

The source Compose publish rule defaults to
`127.0.0.1:${APP_PORT:-8080}:8001`. Standard bridge networking keeps
`UVICORN_HOST=0.0.0.0` inside the container while exposing only the host
loopback address. The NML deployment uses host networking, so its protected
environment must instead set `UVICORN_HOST=127.0.0.1`; host networking ignores
Compose port-publish bindings. Confirm that the Tailnet address cannot reach
port 8001 before enabling the verifier or proxy.

Existing anonymous jobs are keyed to the browser cookie, while authenticated
jobs are keyed to the stable CAIL subject. Enabling the boundary therefore
makes pre-activation anonymous jobs invisible through the authenticated API.
The files and rows are not deleted and still expire under the existing
12-hour job TTL. Activate after that window has drained or explicitly accept
the short transition during the maintenance window.

## Local verification

```sh
cd backend
uv run --frozen ruff check \
  app/services/cail_identity.py \
  app/services/anonymous_sessions.py \
  app/main.py \
  app/config.py \
  tests/test_cail_identity.py
uv run --frozen pytest -q tests/test_cail_identity.py tests/test_main.py
```

The tests generate disposable RSA keys in memory. They do not use or require
the live CUNY client secret or a production CAIL signing key.
