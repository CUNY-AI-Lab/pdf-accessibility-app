/**
 * What happened to people's PDFs: reads the Worker's `job_finished` events
 * (src/outcome.ts) from Workers Observability for a time window and prints
 * how many uploads finished complete, held for manual remediation, or
 * failed; which release gate checks held them; durations, pages and model
 * use. Read-only. Workers Logs keep seven days.
 *
 *   bun dev/outcomes.ts [--since 2026-10-28T00:00:00Z | --days 7] [--until ...]
 *     [--price-in 0.53 --price-out 2.66] [--json]
 *
 * Needs CLOUDFLARE_API_TOKEN (Workers Observability read) and
 * CLOUDFLARE_ACCOUNT_ID. The Gateway does not report a cost per call, so the
 * events' cost is usually 0; --price-in and --price-out (USD per million
 * prompt and completion tokens, the model's catalog price) estimate it from
 * the tokens.
 */
import { parseArgs } from "node:util";
import { z } from "zod";
import { JOB_FINISHED, type JobFinished, jobFinishedSchema } from "../src/outcome";

const SERVICE = "cail-pdf-accessibility";
const PAGE = 2000;
const DAY = 24 * 3600 * 1000;

const { values: options } = parseArgs({
  options: {
    since: { type: "string" },
    until: { type: "string" },
    days: { type: "string", default: "7" },
    "price-in": { type: "string" },
    "price-out": { type: "string" },
    json: { type: "boolean", default: false },
  },
});

const token = process.env["CLOUDFLARE_API_TOKEN"] ?? "";
const account = process.env["CLOUDFLARE_ACCOUNT_ID"] ?? "";
if (token === "" || account === "") {
  console.error("Set CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID.");
  process.exit(2);
}
const until = options.until === undefined ? Date.now() : Date.parse(options.until);
const since = options.since === undefined ? until - Number(options.days) * DAY : Date.parse(options.since);
if (!Number.isFinite(since) || !Number.isFinite(until) || since >= until) {
  console.error("The window is not a valid time range.");
  process.exit(2);
}

const responseSchema = z.object({
  success: z.boolean(),
  errors: z.array(z.object({ message: z.string() })),
  result: z.object({ events: z.object({ events: z.array(z.object({ source: z.json(), timestamp: z.number() })) }) }).optional(),
});

/** The events between two instants; a window that fills a page is split in two. */
async function query(from: number, to: number): Promise<JobFinished[]> {
  const response = await fetch(`https://api.cloudflare.com/client/v4/accounts/${account}/workers/observability/telemetry/query`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify({
      queryId: "pdf-accessibility-job-finished",
      view: "events",
      limit: PAGE,
      timeframe: { from, to },
      parameters: {
        filters: [
          { key: "$metadata.service", operation: "eq", type: "string", value: SERVICE },
          { key: "event", operation: "eq", type: "string", value: JOB_FINISHED },
        ],
      },
    }),
  });
  const body = responseSchema.parse(await response.json());
  if (!response.ok || !body.success || body.result === undefined) {
    throw new Error(`Observability query answered ${response.status}: ${body.errors.map((error) => error.message).join("; ")}`);
  }
  const events = body.result.events.events;
  if (events.length >= PAGE && to - from > 1000) {
    const middle = Math.floor((from + to) / 2);
    return [...(await query(from, middle)), ...(await query(middle, to))];
  }
  return events.flatMap((event) => {
    const parsed = jobFinishedSchema.safeParse(event.source);
    return parsed.success ? [parsed.data] : [];
  });
}

/** The API answers at most seven days at a time. */
async function window(from: number, to: number): Promise<JobFinished[]> {
  const events: JobFinished[] = [];
  for (let start = from; start < to; start += 7 * DAY) events.push(...(await query(start, Math.min(start + 7 * DAY, to))));
  return events;
}

function median(values: number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 1 ? (sorted[middle] ?? null) : ((sorted[middle - 1] ?? 0) + (sorted[middle] ?? 0)) / 2;
}

function tally(keys: string[]): [string, number][] {
  const counts = new Map<string, number>();
  for (const key of keys) counts.set(key, (counts.get(key) ?? 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

const share = (part: number, whole: number): string => (whole === 0 ? "-" : `${((100 * part) / whole).toFixed(0)}%`);

const events = await window(since, until);
const uploads = events.filter((event) => event.kind === "process");
const reviews = events.filter((event) => event.kind === "review");
const held = uploads.filter((event) => event.status === "manual_remediation");
const failed = uploads.filter((event) => event.status === "failed");
const usage = uploads.flatMap((event) => (event.model_usage === null ? [] : [event.model_usage]));
const promptTokens = usage.reduce((sum, item) => sum + item.prompt_tokens, 0);
const completionTokens = usage.reduce((sum, item) => sum + item.completion_tokens, 0);
const reportedCost = usage.reduce((sum, item) => sum + item.cost_usd, 0);
const priceIn = Number(options["price-in"] ?? Number.NaN);
const priceOut = Number(options["price-out"] ?? Number.NaN);
const estimatedCost = (promptTokens * priceIn + completionTokens * priceOut) / 1e6;
const pages = uploads.flatMap((event) => (event.pages === null ? [] : [event.pages]));

const summary = {
  window: { since: new Date(since).toISOString(), until: new Date(until).toISOString() },
  uploads: uploads.length,
  reviews: reviews.length,
  complete: uploads.filter((event) => event.status === "complete").length,
  manual_remediation: held.length,
  failed: failed.length,
  held_by: tally(held.flatMap((event) => (event.held_by.length > 0 ? event.held_by : ["(no failed check)"]))),
  held_with_hidden_tasks: held.filter((event) => (event.hidden_blocking_tasks ?? 0) > 0).length,
  review_tasks: tally(held.flatMap((event) => Object.keys(event.review_tasks))),
  failures: tally(failed.map((event) => `${event.failure ?? "unknown"} at ${event.failed_step ?? "-"}`)),
  classification: tally(uploads.map((event) => event.classification ?? "unknown")),
  median_seconds: median(uploads.map((event) => event.seconds)),
  median_pages: median(pages),
  total_pages: pages.reduce((sum, value) => sum + value, 0),
  model_requests: usage.reduce((sum, item) => sum + item.requests, 0),
  prompt_tokens: promptTokens,
  completion_tokens: completionTokens,
  reported_cost_usd: reportedCost,
  estimated_cost_usd: Number.isFinite(estimatedCost) ? estimatedCost : null,
};

if (options.json) {
  console.log(JSON.stringify({ summary, events }, null, 1));
} else {
  const lines = [
    `${SERVICE}: job_finished ${summary.window.since} to ${summary.window.until}`,
    `uploads ${summary.uploads} (reviews ${summary.reviews})`,
    `  complete            ${summary.complete} (${share(summary.complete, summary.uploads)})`,
    `  manual remediation  ${summary.manual_remediation} (${share(summary.manual_remediation, summary.uploads)})`,
    `  failed              ${summary.failed} (${share(summary.failed, summary.uploads)})`,
    `held by (check: held uploads)`,
    ...summary.held_by.map(([check, count]) => `  ${check.padEnd(28)}${count} (${share(count, held.length)})`),
    `  held with blocking tasks the review screen does not show: ${summary.held_with_hidden_tasks}`,
    `review tasks on held uploads (type: uploads)`,
    ...summary.review_tasks.map(([type, count]) => `  ${type.padEnd(28)}${count}`),
    `failures (reason at step: uploads)`,
    ...summary.failures.map(([reason, count]) => `  ${reason.padEnd(28)}${count}`),
    `classification ${summary.classification.map(([kind, count]) => `${kind} ${count}`).join(", ") || "-"}`,
    `median seconds ${summary.median_seconds ?? "-"}, median pages ${summary.median_pages ?? "-"}, pages ${summary.total_pages}`,
    `model requests ${summary.model_requests}, tokens ${promptTokens} in / ${completionTokens} out,` +
      ` cost reported $${reportedCost.toFixed(4)}` +
      (summary.estimated_cost_usd === null ? "" : `, estimated $${summary.estimated_cost_usd.toFixed(4)}`),
  ];
  console.log(lines.join("\n"));
}
