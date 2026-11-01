// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { AppliedChange, Job, ReviewTask, ValidationReport } from "../types";
import type { ReviewContext } from "../state/jobView";
import ReviewPage from "./ReviewPage";

const api = vi.hoisted(() => ({ job: {} as Job, context: {} as ReviewContext<{ tasks: ReviewTask[]; changes: AppliedChange[]; figures: AppliedChange[]; validation: ValidationReport }>, keep: vi.fn(), edit: vi.fn(), resolve: vi.fn(), undo: vi.fn(), revise: vi.fn() }));
vi.mock("../api/jobs", () => ({
  useJob: () => ({ data: api.job, isLoading: false, error: null }),
  useReviewContext: () => api.context,
  useKeepAppliedChange: () => ({ mutateAsync: api.keep }),
  useEditAppliedChange: () => ({ mutateAsync: api.edit }),
  useResolveReviewTask: () => ({ mutateAsync: api.resolve }),
  useUndoAppliedChange: () => ({ mutateAsync: api.undo }),
  useReviseAppliedChange: () => ({ mutateAsync: api.revise }),
}));
let host: HTMLDivElement, root: Root;
const change = (id: number): AppliedChange => ({ id, change_type: "figure_semantics", title: `Figure ${id}`, detail: "", importance: "low", review_status: "pending_review", reviewable: true, metadata: {}, before: {}, after: { generated_text: `description ${id}` } });
const ready = (): typeof api.context => ({ status: "ready", revision: "r1", data: { tasks: [{ id: 10, task_type: "content_fidelity", title: "Check", detail: "", severity: "high", blocking: true, status: "pending_review", source: "fidelity", metadata: {} }], changes: [change(1), change(2)], figures: [], validation: { compliant: true, violations: [], summary: {} } } });
function render() { act(() => root.render(<MemoryRouter><ReviewPage /></MemoryRouter>)); }
function buttons(name: string) { return [...host.querySelectorAll("button")].filter((node) => node.textContent?.trim() === name); }
beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  api.job = { id: "job", filename: "a.pdf", original_filename: "a.pdf", status: "complete", validation_compliant: true, created_at: "0", updated_at: "1", steps: [] };
  api.context = ready();
  for (const fn of [api.keep, api.edit, api.resolve, api.undo, api.revise]) fn.mockReset();
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); });

it("serializes same-kind and resolve/edit operations at the job boundary, including two clicks before render", async () => {
  let finish!: (value: { job_status: string }) => void;
  const pending = new Promise<{ job_status: string }>((resolve) => { finish = resolve; });
  api.keep.mockReturnValueOnce(pending);
  const log = vi.spyOn(console, "error").mockImplementation(() => {});
  render();
  const keep = buttons("Keep");
  act(() => { keep[0].click(); keep[1].click(); });
  expect(api.keep).toHaveBeenCalledTimes(1);
  expect(log).toHaveBeenCalled();
  expect(buttons("Edit").every((button) => button.disabled)).toBe(true);
  expect(buttons("Mark as Resolved")[0].disabled).toBe(true);
  await act(async () => { finish({ job_status: "complete" }); await pending; });
  expect(buttons("Edit").every((button) => !button.disabled)).toBe(true);
});
it("holds a job-wide draft lock and preserves the draft across context loading and a changed record", () => {
  render();
  act(() => buttons("Edit")[0].click());
  expect(buttons("Keep").every((button) => button.disabled)).toBe(true);
  expect(buttons("Mark as Resolved")[0].disabled).toBe(true);
  expect(buttons("Edit")[0].disabled).toBe(true);
  api.context = { status: "loading", revision: "r2" };
  api.job = { ...api.job, updated_at: "2" };
  render();
  api.context = ready();
  render();
  expect(host.querySelector("textarea")!.value).toBe("description 1");
  expect(buttons("Save")[0].disabled).toBe(true);
  expect(api.edit).not.toHaveBeenCalled();
  act(() => buttons("Cancel draft")[0].click());
  expect(buttons("Keep").every((button) => !button.disabled)).toBe(true);
});
it("shows a failed resolve as an error and releases its busy state for retry", async () => {
  api.resolve.mockRejectedValue(new Error("resolve refused"));
  render();
  await act(async () => { buttons("Mark as Resolved")[0].click(); await Promise.resolve(); });
  expect(host.querySelector("[role=alert]")?.textContent).toContain("resolve refused");
  expect(buttons("Mark as Resolved")[0].disabled).toBe(false);
});
