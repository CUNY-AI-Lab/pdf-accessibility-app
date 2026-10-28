import { TEST_SUBJECTS } from "@cuny-ai-lab/cail-identity/testing";
import { SELF } from "cloudflare:test";
import { env } from "cloudflare:workers";
import { importJWK, SignJWT } from "jose";
import { z } from "zod";

export const ORIGIN = "https://tools.ailab.gc.cuny.edu";
export const PDF = new TextEncoder().encode("%PDF-1.7\n% test document\n");
export const { alice, bob } = TEST_SUBJECTS;

async function sign(audience: string, subject: string, lifetimeSeconds: number): Promise<string> {
  const key = await importJWK(JSON.parse(env.TEST_PRIVATE_JWK), "RS256");
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({})
    .setProtectedHeader({ alg: "RS256", kid: "pdf-accessibility-test", typ: "JWT" })
    .setIssuer(env.CAIL_IDENTITY_ISSUER)
    .setAudience(audience)
    .setSubject(subject)
    .setIssuedAt(now)
    .setExpirationTime(now + lifetimeSeconds)
    .sign(key);
}

/** The headers Doorway adds for a signed-in person. */
export async function signedIn(subject: string = alice, gatewaySeconds = 24 * 3600): Promise<Headers> {
  return new Headers({
    "x-cail-identity-jwt": await sign("cail:pdf-accessibility", subject, 300),
    "x-cail-gateway-identity-jwt": await sign("cail:gateway", subject, gatewaySeconds),
  });
}

export async function api(path: string, init: RequestInit & { as?: Headers } = {}): Promise<Response> {
  const headers = new Headers(init.as ?? (await signedIn()));
  new Headers(init.headers).forEach((value, name) => headers.set(name, value));
  return SELF.fetch(`${ORIGIN}/pdf-accessibility/api${path}`, { ...init, headers });
}

export async function upload(as?: Headers, name = "syllabus.pdf", bytes: Uint8Array = PDF): Promise<Response> {
  return api(`/jobs?filename=${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "content-type": "application/pdf", "content-length": String(bytes.length), origin: ORIGIN },
    body: bytes,
    as: as ?? (await signedIn()),
  });
}

export function runner(path: string, init: RequestInit & { lease?: string } = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (!headers.has("x-pdf-runner-token")) headers.set("x-pdf-runner-token", "runner-secret");
  if (init.lease !== undefined) headers.set("x-pdf-job-token", init.lease);
  return SELF.fetch(`${ORIGIN}/pdf-accessibility/runner${path}`, { ...init, headers });
}

export const taskSchema = z.object({
  job_id: z.string(),
  kind: z.string(),
  lease: z.string(),
  attempt: z.number(),
  deadline: z.number(),
  original_filename: z.string(),
  action: z.object({ type: z.string(), change_id: z.number(), text: z.string().optional() }).nullable(),
  overrides: z.object({ changes: z.record(z.string(), z.string()), tasks: z.record(z.string(), z.string()) }),
  overrides_version: z.number(),
});
export type TestTask = z.infer<typeof taskSchema>;

export async function claim(): Promise<TestTask | null> {
  const response = await runner("/claim", { method: "POST", body: JSON.stringify({ runner: "test-host" }) });
  if (response.status === 204) return null;
  return z.object({ task: taskSchema }).parse(await response.json()).task;
}

export const jobSchema = z.object({
  id: z.string(),
  status: z.string(),
  error: z.string().nullable(),
  notice: z.string().nullable(),
  original_filename: z.string(),
  page_count: z.number().nullable(),
  steps: z.array(z.object({ step_name: z.string(), status: z.string(), result: z.record(z.string(), z.unknown()).nullable() })),
});

export async function createdJobId(response: Response): Promise<string> {
  const body = z.object({ jobs: z.array(jobSchema) }).parse(await response.json());
  const job = body.jobs[0];
  if (job === undefined) throw new Error("no job created");
  return job.id;
}

export async function job(id: string, as?: Headers): Promise<z.infer<typeof jobSchema>> {
  const response = await api(`/jobs/${id}`, as === undefined ? {} : { as });
  return jobSchema.parse(await response.json());
}

const STEP_NAMES = ["classify", "ocr", "structure", "alt_text", "tagging", "validation", "fidelity"];

/** What a runner reports for a finished job: its JobResponse and the files it uploaded. */
export function finishedJob(status: string) {
  return {
    status,
    classification: "digital",
    ocr_language: "eng",
    page_count: 4,
    error: null,
    validation_compliant: status === "complete",
    steps: STEP_NAMES.map((step_name) => ({
      step_name,
      status: "complete",
      started_at: "2026-10-27T12:00:00Z",
      completed_at: "2026-10-27T12:01:00Z",
      error: null,
      result: { step: step_name },
    })),
  };
}

export const CHANGES = [
  {
    id: 1,
    change_type: "figure_semantics",
    title: "Described figure 1",
    detail: "Generated alt text.",
    importance: "medium",
    review_status: "pending_review",
    reviewable: true,
    metadata: { figure_index: 0 },
    before: {},
    after: { edited_text: "A bar chart" },
  },
];

export const TASKS = [
  {
    id: 7,
    task_type: "alt_text",
    title: "Check figure descriptions",
    detail: "Confirm the generated alt text.",
    severity: "medium",
    blocking: false,
    status: "pending_review",
    source: "fidelity",
    metadata: { pages_to_check: [2] },
  },
];

export async function putFile(task: TestTask, name: string, body: BodyInit, type: string): Promise<Response> {
  return runner(`/tasks/${task.job_id}/files/${name}`, {
    method: "PUT",
    lease: task.lease,
    headers: { "content-type": type },
    body,
  });
}

/** Run a claimed process task to a reviewable result, as the runner would. */
export async function finishProcess(task: TestTask, status = "manual_remediation"): Promise<Response> {
  const files: [string, string, string][] = [
    ["views/applied-changes.json", JSON.stringify(CHANGES), "application/json"],
    ["views/figure-changes.json", JSON.stringify(CHANGES), "application/json"],
    ["views/review-tasks.json", JSON.stringify(TASKS), "application/json"],
    ["views/report.json", JSON.stringify({ compliant: false }), "application/json"],
    [
      "views/batch-parts.json",
      JSON.stringify({ styles: "<style></style>", header: "<header></header>", footer: "<footer></footer>", row: "<tr><td>syllabus.pdf</td></tr>", section: "<div class=\"job-section\">syllabus</div>" }),
      "application/json",
    ],
    ["report.html", "<html>report</html>", "text/html; charset=utf-8"],
    ["output.pdf", "%PDF-1.7 tagged", "application/pdf"],
    ["previews/2", "png-bytes", "image/png"],
    ["figures/0", "figure-bytes", "image/png"],
    ["state.tar.gz", "state-bytes", "application/gzip"],
  ];
  for (const [name, body, type] of files) {
    const response = await putFile(task, name, body, type);
    if (response.status !== 204) throw new Error(`upload of ${name} answered ${response.status}`);
  }
  return runner(`/tasks/${task.job_id}/complete`, {
    method: "POST",
    lease: task.lease,
    body: JSON.stringify({
      job: finishedJob(status),
      figures: [0],
      previews: [2],
      files: files.map(([name]) => name).filter((name) => name !== "state.tar.gz"),
      has_state: true,
      overrides_version: task.overrides_version,
    }),
  });
}
