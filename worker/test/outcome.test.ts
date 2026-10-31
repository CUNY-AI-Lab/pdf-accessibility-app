import { runInDurableObject } from "cloudflare:test";
import { env } from "cloudflare:workers";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LIMITS } from "../src/board";
import type { JsonValue } from "../src/contract";
import { jobBoard } from "../src/env";
import { type JobFinished, jobFinishedSchema } from "../src/outcome";
import { alice, api, claim, createdJobId, finishedJob, finishProcess, ORIGIN, putFile, runner, type TestTask, upload } from "./helpers";

const usage = (requests: number) => ({
  request_count: requests,
  prompt_tokens: 1000 * requests,
  completion_tokens: 100 * requests,
  total_tokens: 1100 * requests,
  cost_usd: 0.001 * requests,
});


let logged: string[] = [];

beforeEach(async () => {
  await runInDurableObject(jobBoard(env), (_board, state) => {
    state.storage.sql.exec("DELETE FROM jobs");
  });
  const stored = await env.FILES.list({ prefix: "jobs/" });
  if (stored.objects.length > 0) await env.FILES.delete(stored.objects.map((object) => object.key));
  logged = [];
  vi.spyOn(console, "log").mockImplementation((line: string) => {
    logged.push(line);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

function finishedEvents(): JobFinished[] {
  return logged
    .filter((line) => line.includes('"job_finished"'))
    .map((line) => jobFinishedSchema.parse(JSON.parse(line)));
}

async function claimed(): Promise<TestTask> {
  const task = await claim();
  if (task === null) throw new Error("no task");
  return task;
}

/** A held result as the runner reports it: two review tasks, two failed gate checks, model use in three steps. */
async function completeHeld(task: TestTask): Promise<Response> {
  const tasks = [
    { id: 1, task_type: "alt_text", title: "Describe figures in syllabus.pdf", detail: "Figure 2 shows enrollment", severity: "high", blocking: true, status: "pending_review", source: "fidelity", metadata: { pages_to_check: [2] } },
    { id: 2, task_type: "alt_text", title: "Check figure descriptions", detail: "", severity: "medium", blocking: false, status: "pending_review", source: "fidelity", metadata: {} },
    { id: 3, task_type: "table_semantics", title: "Check table headers", detail: "", severity: "high", blocking: true, status: "pending_review", source: "fidelity", metadata: {} },
  ];
  const report = {
    compliant: false,
    violations: [{ rule_id: "ISO 14289-1:2014-7.3-1", description: "Figure without alt", severity: "error" }],
    fidelity: {
      passed: false,
      checks: [
        { check: "compliance", status: "fail", message: "1 rule", metrics: {} },
        { check: "reading_order", status: "pass", message: "", metrics: {} },
        { check: "figure_descriptions", status: "fail", message: "", metrics: {} },
        { check: "link_text_quality", status: "warning", message: "", metrics: {} },
        { check: "tagged_text_coverage", status: "error", message: "The check could not run", metrics: {} },
      ],
    },
  };
  const view = finishedJob("manual_remediation");
  const results = new Map<string, { [key: string]: JsonValue }>([
    ["classify", { type: "scanned", total_pages: 4 }],
    ["structure", { figures: 2, structure_intelligence_llm_usage: usage(2), title_intelligence_assist: { llm_usage: usage(1) } }],
    ["alt_text", { count: 2, llm_usage: usage(3) }],
    ["tagging", { title_set: true, pretag_llm_usage: usage(1), grounded_text_audit: { llm_usage: usage(1) } }],
    ["fidelity", { passed: false, blocking_tasks: 4, advisory_tasks: 1 }],
  ]);
  const job = {
    ...view,
    classification: "scanned",
    steps: view.steps.map((step) => ({
      ...step,
      completed_at: step.step_name === "structure" ? "2026-10-27T12:02:30Z" : step.completed_at,
      result: results.get(step.step_name) ?? null,
    })),
  };
  await putFile(task, "views/review-tasks.json", JSON.stringify(tasks), "application/json");
  await putFile(task, "views/report.json", JSON.stringify(report), "application/json");
  return runner(`/tasks/${task.job_id}/complete`, {
    method: "POST",
    lease: task.lease,
    body: JSON.stringify({
      job,
      figures: [],
      previews: [],
      files: ["views/review-tasks.json", "views/report.json"],
      has_state: false,
      overrides_version: task.overrides_version,
    }),
  });
}

describe("job_finished", () => {
  it("reports a held result's status, gate checks, review tasks, timings and model use", async () => {
    const id = await createdJobId(await upload());
    const task = await claimed();
    expect((await completeHeld(task)).status).toBe(204);

    const events = finishedEvents();
    expect(events).toHaveLength(1);
    const [event] = events;
    expect(event).toMatchObject({
      job: id,
      kind: "process",
      attempt: 1,
      status: "manual_remediation",
      failure: null,
      failed_step: null,
      classification: "scanned",
      pages: 4,
      validation_compliant: false,
      blocking_tasks: 4,
      advisory_tasks: 1,
      review_tasks: { alt_text: { total: 2, blocking: 1 }, table_semantics: { total: 1, blocking: 1 } },
      hidden_blocking_tasks: 2,
      held_by: ["compliance", "figure_descriptions", "tagged_text_coverage"],
      // Nested audits are already in their step's total; counting them again would double the bill.
      model_usage: { requests: 6, prompt_tokens: 6000, completion_tokens: 600 },
    });
    expect(event?.model_usage?.cost_usd).toBeCloseTo(0.006);
    expect(event?.steps["structure"]).toEqual({ status: "complete", seconds: 150 });
    expect(Object.keys(event?.steps ?? {})).toHaveLength(7);
  });

  it("carries no file name, person, task text or document text", async () => {
    await upload();
    await completeHeld(await claimed());
    const line = logged.find((entry) => entry.includes('"job_finished"')) ?? "";
    expect(line).not.toBe("");
    for (const secret of ["syllabus", alice, "enrollment", "Describe figures", "Figure without alt", "could not run"]) {
      expect(line).not.toContain(secret);
    }
  });

  it("reports a complete result as held by nothing, and does not report a re-render", async () => {
    const id = await createdJobId(await upload());
    expect((await finishProcess(await claimed(), "complete")).status).toBe(204);
    expect(finishedEvents().map((event) => [event.status, event.held_by])).toEqual([["complete", []]]);

    await api(`/jobs/${id}/review-tasks/7/resolve`, { method: "POST", headers: { origin: ORIGIN } });
    const sync = await claimed();
    expect(sync.kind).toBe("sync");
    expect((await finishProcess(sync, "complete")).status).toBe(204);
    expect(finishedEvents()).toHaveLength(1);
  });

  it("still completes when the uploaded views cannot be read", async () => {
    await upload();
    const task = await claimed();
    await putFile(task, "views/review-tasks.json", "not json", "application/json");
    await putFile(task, "views/report.json", JSON.stringify({ fidelity: "unexpected" }), "application/json");
    const done = await runner(`/tasks/${task.job_id}/complete`, {
      method: "POST",
      lease: task.lease,
      body: JSON.stringify({
        job: finishedJob("manual_remediation"),
        figures: [],
        previews: [],
        files: ["views/review-tasks.json", "views/report.json"],
        has_state: false,
        overrides_version: 0,
      }),
    });
    expect(done.status).toBe(204);
    expect(finishedEvents().map((event) => [event.review_tasks, event.held_by])).toEqual([[{}, []]]);
  });

  it("reports a runner's failure with the step it stopped in, without the error", async () => {
    await upload();
    const task = await claimed();
    for (const event of [
      { step: "classify", status: "running" },
      { step: "classify", status: "complete", result: { type: "digital" } },
      { step: "ocr", status: "skipped" },
      { step: "structure", status: "running" },
    ]) {
      await runner(`/tasks/${task.job_id}/progress`, { method: "POST", lease: task.lease, body: JSON.stringify(event) });
    }
    const failed = await runner(`/tasks/${task.job_id}/fail`, {
      method: "POST",
      lease: task.lease,
      body: JSON.stringify({ error: "syllabus.pdf has 900 pages; the limit is 300." }),
    });
    expect(failed.status).toBe(204);
    expect(finishedEvents()).toEqual([
      expect.objectContaining({ status: "failed", failure: "runner", failed_step: "structure", classification: "digital", pages: null }),
    ]);
    expect(logged.join("\n")).not.toContain("900 pages");
  });

  it("reports a job the Worker gave up on after its runner went quiet twice", async () => {
    await upload();
    const board = jobBoard(env);
    await claimed();
    await board.sweep(Date.now() + LIMITS.leaseMs + 1000);
    await claimed();
    expect(finishedEvents()).toHaveLength(0);
    await board.sweep(Date.now() + LIMITS.leaseMs + 1000);
    expect(finishedEvents()).toEqual([
      expect.objectContaining({ status: "failed", failure: "interrupted", attempt: 2, failed_step: "classify" }),
    ]);
  });

  it("counts only a review's re-run steps", async () => {
    const id = await createdJobId(await upload());
    await finishProcess(await claimed());
    await api(`/jobs/${id}/applied-changes/1/undo`, { method: "POST", headers: { origin: ORIGIN } });
    const review = await claimed();
    expect(review.kind).toBe("review");
    expect((await completeHeld(review)).status).toBe(204);
    const event = finishedEvents().at(-1);
    expect(event?.kind).toBe("review");
    expect(Object.keys(event?.steps ?? {})).toEqual(["tagging", "validation", "fidelity"]);
    expect(event?.model_usage?.requests).toBe(1);
  });
});
