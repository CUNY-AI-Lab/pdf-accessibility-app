import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import type { Job, PipelineStep } from "../types";
import { complianceOutcome, downloadCapabilities, JOB_STATUSES, TERMINAL_JOB_STATUSES, jobPollInterval, jobProgress, jobRevision, knownJobStatus, loadReviewSnapshot, outputReady, projectJobView, projectReviewContext } from "./jobView";
export const job = (status = "complete", updated_at = "1"): Job => ({ id: "job", filename: "a.pdf", original_filename: "a.pdf", status, created_at: "0", updated_at, steps: [] });

describe("remote job projections", () => {
  it("uses exactly the deployed Worker job and terminal vocabulary", () => {
    const source = readFileSync(new URL("../../../worker/src/contract.ts", import.meta.url), "utf8");
    const values = (name: string) => source.match(new RegExp(`export const ${name} = z.enum\\(\\[([^\\]]+)\\]\\)`))![1].match(/"[^"]+"/g)!.map((value) => JSON.parse(value));
    expect([...JOB_STATUSES].sort()).toEqual(values("jobStatusSchema").sort());
    expect([...TERMINAL_JOB_STATUSES].sort()).toEqual(values("terminalJobStatusSchema").sort());
  });
  it.each([
    ["queued", "active", false], ["processing", "active", false], ["complete", "output_ready", true],
    ["manual_remediation", "output_ready", true], ["failed", "failed", false], ["future", "unknown", false],
  ])("projects %s without writing remote lifecycle", (status, view, ready) => {
    expect(projectJobView(job(status), false, null).status).toBe(view);
    expect(outputReady(job(status))).toBe(ready);
    expect(downloadCapabilities({ ...job(status), has_html: true })).toEqual({ pdf: ready, report: ready, html: ready });
    expect(knownJobStatus(status)).toBe(status === "future" ? "unknown" : status);
  });
  it("rechecks terminal, unknown and failed initial requests; detects external rerun and expiry", () => {
    for (const status of JOB_STATUSES) expect(jobPollInterval(job(status))).toBeGreaterThan(0);
    expect(jobPollInterval()).toBe(30000);
    expect(jobPollInterval(job("future"))).toBe(30000);
    expect(projectJobView(undefined, true, null)).toEqual({ status: "loading" });
    expect(projectJobView(undefined, false, new Error("offline"))).toEqual({ status: "unavailable", reason: "request" });
    expect(projectJobView(job(), false, Object.assign(new Error("expired"), { status: 404 }))).toEqual({ status: "unavailable", reason: "missing" });
    expect(projectJobView(job("processing", "2"), false, null).status).toBe("active");
  });
  it("uses seven canonical steps for partial and legacy step arrays", () => {
    const steps = [{ step_name: "classify", status: "complete" }, { step_name: "ocr", status: "skipped" }, { step_name: "tagging", status: "future" }] as PipelineStep[];
    expect(jobProgress({ ...job(), steps })).toEqual({ completed: 2, total: 7, percent: 2 / 7 * 100 });
    expect(jobProgress(job()).percent).toBe(0);
    expect(downloadCapabilities(job()).html).toBe(false);
  });
});

describe("review context", () => {
  it("never treats absent or failed validation, old revisions or disabled cached data as ready", () => {
    const current = job();
    const cached = { revision: jobRevision(current), data: { validation: true } };
    expect(projectReviewContext(current, { error: null, isFetching: true }).status).toBe("loading");
    expect(projectReviewContext(current, { data: cached, error: new Error("validation failed"), isFetching: false }).status).toBe("unavailable");
    expect(projectReviewContext(job("complete", "2"), { data: cached, error: null, isFetching: false }).status).toBe("loading");
    expect(projectReviewContext(job("processing"), { data: cached, error: null, isFetching: false }).status).toBe("not_applicable");
    expect(projectReviewContext(current, { data: cached, error: null, isFetching: false })).toMatchObject({ status: "ready", data: cached.data });
  });
  it("publishes only a completely loaded bundle bracketed by the same job identity", async () => {
    const current = job();
    const data = { tasks: [], validation: { compliant: false } };
    const result = await loadReviewSnapshot(current, async () => current, async () => data);
    expect(result).toEqual({ revision: jobRevision(current), data });
    await expect(loadReviewSnapshot(current, async () => current, async () => { throw new Error("missing report"); })).rejects.toThrow("missing report");
  });
  it("rejects revision skew before or during the bundle and output disappearing", async () => {
    const current = job();
    const views = vi.fn(async () => ({ tasks: [] }));
    await expect(loadReviewSnapshot(current, async () => job("complete", "2"), views)).rejects.toThrow("before");
    expect(views).not.toHaveBeenCalled();
    const read = vi.fn().mockResolvedValueOnce(current).mockResolvedValueOnce(job("complete", "2"));
    await expect(loadReviewSnapshot(current, read, views)).rejects.toThrow("while");
    const rerun = vi.fn().mockResolvedValueOnce(current).mockResolvedValueOnce(job("processing"));
    await expect(loadReviewSnapshot(current, rerun, views)).rejects.toThrow("while");
  });
});

it("does not equate a complete job with a verified compliant output", () => {
  expect(complianceOutcome("complete", false, "ready")).toBe("unverified");
  expect(complianceOutcome("complete", true, "loading")).toBe("unverified");
  expect(complianceOutcome("complete", true, "unavailable")).toBe("unverified");
  expect(complianceOutcome("complete", true, "ready")).toBe("passed");
  expect(complianceOutcome("manual_remediation", true, "ready")).toBe("needs_fixes");
});
