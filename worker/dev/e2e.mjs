// Local end to end: the real Worker (wrangler dev) and the real runner
// (python -m app.runner) on one PDF, with identity legs signed by a local key
// and model calls sent to the Gateway with the developer's own key.
//
//   DEV_GATEWAY_KEY=sk-cail-... DOCLING_SERVE_URL=... node dev/e2e.mjs some.pdf
//
// It uploads the PDF, waits for the runner to finish it, reads every view,
// keeps one figure decision, edits one, and checks each result.
import { spawn } from "node:child_process";
import { generateKeyPairSync, createHash } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join, resolve } from "node:path";
import { importJWK, SignJWT } from "jose";
import { z } from "zod";

const PORT = 8791;
const ORIGIN = `http://127.0.0.1:${PORT}`;
const BASE = `${ORIGIN}/pdf-accessibility`;
const ISSUER = "https://tools.ailab.gc.cuny.edu/cail-sso";
const RUNNER_TOKEN = `dev-${Date.now()}`;
const workerDir = resolve(import.meta.dirname, "..");
const backendDir = resolve(workerDir, "../backend");
const pdfPath = resolve(process.argv[2] ?? "");
const gatewayKey = process.env.DEV_GATEWAY_KEY ?? "";
if (!process.argv[2] || !gatewayKey) {
  console.error("usage: DEV_GATEWAY_KEY=... node dev/e2e.mjs file.pdf");
  process.exit(2);
}

const { publicKey, privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const kid = "pdf-accessibility-dev";
const jwks = { keys: [{ ...publicKey.export({ format: "jwk" }), kid, alg: "RS256", use: "sig" }] };
const signingKey = await importJWK({ ...privateKey.export({ format: "jwk" }), alg: "RS256" }, "RS256");
const subject = `cail-${createHash("sha256").update("e2e").digest("hex").slice(0, 32)}`;

async function sign(audience, seconds) {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({})
    .setProtectedHeader({ alg: "RS256", kid, typ: "JWT" })
    .setIssuer(ISSUER)
    .setAudience(audience)
    .setSubject(subject)
    .setIssuedAt(now)
    .setExpirationTime(now + seconds)
    .sign(signingKey);
}

async function person() {
  return {
    "x-cail-identity-jwt": await sign("cail:pdf-accessibility", 300),
    "x-cail-gateway-identity-jwt": await sign("cail:gateway", 24 * 3600),
  };
}

async function api(path, init = {}) {
  const headers = { ...(await person()), ...init.headers };
  if (init.method && init.method !== "GET") headers.origin = "https://tools.ailab.gc.cuny.edu";
  return fetch(`${BASE}/api${path}`, { ...init, headers });
}

async function apiJson(path, init) {
  const response = await api(path, init);
  const body = await response.json();
  if (!response.ok) throw new Error(`${init?.method ?? "GET"} ${path}: ${response.status} ${JSON.stringify(body)}`);
  return body;
}

function check(condition, message) {
  if (!condition) throw new Error(`check failed: ${message}`);
  console.log(`  ok: ${message}`);
}

const sleep = (ms) => new Promise((done) => setTimeout(done, ms));
const scratch = mkdtempSync(join(tmpdir(), "pdf-v2-e2e-"));
const children = [];

function start(command, args, options, label) {
  const child = spawn(command, args, { ...options, stdio: ["ignore", "pipe", "pipe"] });
  const log = join(scratch, `${label}.log`);
  const lines = [];
  for (const stream of [child.stdout, child.stderr]) {
    stream.on("data", (chunk) => {
      lines.push(chunk.toString());
      writeFileSync(log, lines.join(""));
    });
  }
  children.push(child);
  return log;
}

async function waitFor(what, test, timeoutMs, everyMs = 2000) {
  const until = Date.now() + timeoutMs;
  for (;;) {
    const value = await test();
    if (value) return value;
    if (Date.now() > until) throw new Error(`timed out waiting for ${what}`);
    await sleep(everyMs);
  }
}

async function finished(id, notStatus = null) {
  return waitFor(
    `job ${id} to finish`,
    async () => {
      const job = await apiJson(`/jobs/${id}`);
      const running = job.steps.filter((step) => step.status === "running").map((step) => step.step_name);
      process.stdout.write(`\r  ${job.status} ${running.join(",")}`.padEnd(60));
      if (["complete", "manual_remediation", "failed"].includes(job.status) && job.status !== notStatus) {
        process.stdout.write("\n");
        return job;
      }
      return null;
    },
    20 * 60_000,
    3000,
  );
}

try {
  writeFileSync(join(workerDir, ".dev.vars"), `CAIL_IDENTITY_JWKS='${JSON.stringify(jwks)}'\nRUNNER_TOKEN=${RUNNER_TOKEN}\n`);
  writeFileSync(join(workerDir, "dev/.dev.vars"), `DEV_GATEWAY_KEY=${gatewayKey}\n`);
  const workerLog = start(
    "node_modules/.bin/wrangler",
    ["dev", "-c", "wrangler.jsonc", "-c", "dev/gateway.jsonc", "--port", String(PORT), "--persist-to", join(scratch, "state")],
    { cwd: workerDir, env: { ...process.env, WRANGLER_SEND_METRICS: "false" } },
    "worker",
  );
  await waitFor("the Worker", async () => (await fetch(`${BASE}/health`).catch(() => null))?.ok, 60_000, 1000);
  console.log(`Worker up (log ${workerLog})`);

  const bytes = readFileSync(pdfPath);
  const created = await apiJson(`/jobs?filename=${encodeURIComponent(basename(pdfPath))}`, {
    method: "POST",
    headers: { "content-type": "application/pdf" },
    body: bytes,
  });
  const id = created.jobs[0].id;
  check(created.jobs[0].status === "queued", `uploaded ${basename(pdfPath)} as job ${id}`);

  const data = join(scratch, "data");
  const runnerLog = start(
    "uv",
    ["run", "--frozen", "python", "-m", "app.runner"],
    {
      cwd: backendDir,
      env: {
        ...process.env,
        PYTHONPATH: ".",
        RUNNER_WORKER_URL: `${BASE}/runner`,
        RUNNER_TOKEN,
        RUNNER_POLL_SECONDS: "1",
        UPLOAD_DIR: join(data, "uploads"),
        PROCESSING_DIR: join(data, "processing"),
        OUTPUT_DIR: join(data, "output"),
        DATABASE_URL: `sqlite+aiosqlite:///${join(data, "unused.db")}`,
        LLM_API_KEY: "",
      },
    },
    "runner",
  );
  console.log(`Runner started (log ${runnerLog})`);

  const job = await finished(id);
  check(job.status !== "failed", `job finished as ${job.status}${job.error ? `: ${job.error}` : ""}`);
  check(job.steps.every((step) => step.status !== "pending" && step.status !== "running"), "every step reported");

  const download = await api(`/jobs/${id}/download`);
  const pdf = Buffer.from(await download.arrayBuffer());
  check(download.ok && pdf.subarray(0, 5).toString() === "%PDF-", `accessible PDF downloads (${pdf.length} bytes)`);
  check((await api(`/jobs/${id}/download/report.html`)).ok, "HTML report downloads");
  check((await api(`/jobs/download/batch-report.html`)).ok, "batch report downloads");
  const report = z.object({ compliant: z.boolean() }).parse(await apiJson(`/jobs/${id}/download/report`));
  check(true, `validation report (compliant=${report.compliant})`);
  const tasks = await apiJson(`/jobs/${id}/review-tasks`);
  const pending = await apiJson(`/jobs/${id}/applied-changes`);
  const figures = await apiJson(`/jobs/${id}/figure-changes`);
  console.log(`  ${tasks.length} review tasks, ${pending.length} changes to review, ${figures.length} figure decisions`);
  for (const task of tasks) {
    for (const page of task.metadata?.pages_to_check ?? []) {
      check((await api(`/jobs/${id}/pages/${page}/preview`)).ok, `preview of page ${page} for "${task.title}"`);
    }
  }
  for (const figure of figures) {
    const index = z.number().int().safeParse(figure.metadata?.figure_index);
    if (index.success) check((await api(`/jobs/${id}/figures/${index.data}/image`)).ok, `figure ${index.data} image`);
  }

  if (pending.length > 0) {
    const change = pending[0];
    await apiJson(`/jobs/${id}/applied-changes/${change.id}/keep`, { method: "POST" });
    const after = await apiJson(`/jobs/${id}/applied-changes`);
    check(!after.some((item) => item.id === change.id), `kept change ${change.id} leaves the review list`);
  }

  const figure = figures.find((item) => item.change_type === "figure_semantics");
  if (figure) {
    const text = `Edited in the end-to-end check at ${new Date().toISOString()}`;
    const edit = await apiJson(`/jobs/${id}/applied-changes/${figure.id}/edit`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text }),
    });
    check(edit.job_status === "processing", "edit re-runs tagging");
    const rerun = await finished(id);
    check(rerun.status !== "failed", `re-run finished as ${rerun.status}${rerun.notice ? ` (notice: ${rerun.notice})` : ""}`);
    // The edit reaches the figure's alt text, which the report lists. The
    // re-run drops pending change records (v1 behavior), so the figure's
    // latest recorded decision is the original one, marked undone.
    const reportHtml = await (await api(`/jobs/${id}/download/report.html`)).text();
    check(reportHtml.includes(text), "the report lists the edited description");
    const edited = await apiJson(`/jobs/${id}/figure-changes`);
    check(edited.find((item) => item.id === figure.id)?.review_status === "undone", "the edited decision shows as undone");
  }

  await apiJson(`/jobs/${id}`, { method: "DELETE" }).catch(() => null);
  check((await api(`/jobs/${id}`)).status === 404, "deleted job is gone");
  console.log("End to end: all checks passed.");
} catch (error) {
  console.error(`\n${error.message}\nLogs in ${scratch}`);
  process.exitCode = 1;
} finally {
  await Promise.all(
    children.map(
      (child) =>
        new Promise((done) => {
          if (child.exitCode !== null) return done();
          child.once("exit", done);
          child.kill("SIGTERM");
          setTimeout(done, 10_000);
        }),
    ),
  );
  rmSync(join(workerDir, ".dev.vars"), { force: true });
  rmSync(join(workerDir, "dev/.dev.vars"), { force: true });
  if (process.exitCode !== 1) rmSync(scratch, { recursive: true, force: true });
}
