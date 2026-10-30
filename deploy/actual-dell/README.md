# PDF Accessibility v2 on actual-dell

actual-dell (Windows, WSL Ubuntu 24.04, RTX 5000 Ada) runs the v2 pipeline:
the runner in `compose.yml` here and docling-serve in `docling.compose.yml`.
The runner makes only outbound calls. It claims work from the Worker at
`https://tools.ailab.gc.cuny.edu/pdf-accessibility/runner` (Doorway's public
route) with its own token, and sends every model call back through the
Worker, which adds the person's Gateway credential. No Gateway key is on
this machine.

Reach the host with `ssh actual-dell` (Windows) and run commands inside WSL,
for example `ssh actual-dell "wsl -e bash -lc 'docker ps'"`. The services
live under `~/services/` in WSL.

## docling-serve

`~/services/docling/compose.yml` is `docling.compose.yml` from this
directory. To change the version, edit both, then on the host:

```bash
cd ~/services/docling
docker compose pull && docker compose up -d
curl -fsS http://127.0.0.1:5001/version
```

Tailscale Serve also publishes it to the tailnet on port 8444, which
`worker/dev/e2e.mjs` uses.

Memory: on 2026-10-27, after six days up and about 40 conversions in the
last three, docling-serve 1.12.0 held 44 GB in its server process and 10 GB
in 25 idle torch compile workers, of the host's 62 GB. The compose file
limits the compile pool to one thread, shares the models between the two
worker threads, caps the container at 24 GB, and turns on `/v1/memory`.
Check growth with `docker stats --no-stream docling-serve` a day after a
restart and again after a week. Right after the 1.35.0 restart on 2026-10-28
it held 2.4 GB with no compile workers, and the host had 49 GB available.

The container mounts no cache volume: the image carries the models its own
version expects. On 2026-10-28 the old `docling-cache` volume, mounted over
the model directory, hid 1.35.0's models behind 1.12's, and the server
crash-looped for about nine minutes until it was rolled back. The volume and
`compose.yml.bak-20261028` (1.12.0) are kept on the host for a rollback.

## The runner

First install:

```bash
mkdir -p ~/services/pdf-accessibility && cd ~/services/pdf-accessibility
# copy compose.yml and runner.env.example from this directory
cp runner.env.example runner.env   # set RUNNER_TOKEN (the Worker's secret)
chmod 600 runner.env
RUNNER_IMAGE_TAG=<published commit SHA> docker compose up -d
docker compose logs -f runner      # "Runner actual-dell taking work from ..."
```

The image is the app's GHCR image (`Publish container` workflow), published
for a commit on main. To update, run the same `up -d` with the new tag; a
task in progress is lost and retried once by the Worker when its lease
lapses (three minutes).

The runner removes each job's working files after its task. A shared cache
keeps generated figure descriptions keyed by hashes of the image and exact
model request, model ID, and prompt version. It stores no source document
text, captions, images, or job identifiers. The file is
`/app/data/alt_text_cache.sqlite3` in the `runner_data` volume, outside job
archives. Its path follows `PROCESSING_DIR`: it lives in that directory's
parent.

Entries expire 48 hours after storage. Reads do not extend that lifetime.
Expired rows are deleted on cache open, get, and put. The cache holds at most
2,000 descriptions of at most 4,096 UTF-8 bytes each. LRU eviction removes the
least recently used entries when the count exceeds that bound. The SQLite
file is capped at 16 MiB and uses full auto-vacuum.

`ALT_TEXT_CACHE_ENABLED` defaults to `true`. Set
`ALT_TEXT_CACHE_ENABLED=false` in `runner.env` and recreate the runner to
turn it off. With it off, the runner creates no cache file and performs no
cache reads or writes. Any existing cache file is left untouched.

`RUNNER_TOKEN` must equal the Worker's `RUNNER_TOKEN` secret. To rotate it,
set the new value on the Worker, then in `runner.env`, then restart the
runner; tasks claimed under the old token fail their next call and retry.

## Checks

- `docker compose ps` shows the runner healthy (it touches a file every 30
  seconds).
- A job uploaded at `/pdf-accessibility/` moves from queued to processing
  within a few seconds while the runner is up.
- The runner's log names each task it takes; model calls appear as
  `POST .../runner/tasks/<job>/v1/chat/completions`.
