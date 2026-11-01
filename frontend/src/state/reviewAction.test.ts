import { describe, expect, it } from "vitest";
import { actionLeavesReview, reviewActionTransition, type ReviewAction, type ReviewOperation } from "./reviewAction";
const operations: ReviewOperation[] = ["keep", "undo", "revise", "edit", "resolve"];
const start = (operation: ReviewOperation, requestId = "a") => ({ type: "start" as const, operation, requestId, target: 1, revision: "r1", permitted: true });

describe("review action guard", () => {
  it.each(operations)("runs %s through pending to done or failed and permits retry", (operation) => {
    const pending = reviewActionTransition({ status: "idle" }, start(operation));
    expect(pending).toMatchObject({ status: "pending", operation, target: 1, requestId: "a", revision: "r1" });
    const done = reviewActionTransition(pending, { type: "done", requestId: "a" });
    expect(done.status).toBe("done");
    const error = new Error("refused");
    const failed = reviewActionTransition(pending, { type: "failed", requestId: "a", error });
    expect(failed).toMatchObject({ status: "failed", error });
    for (const state of [done, failed]) expect(reviewActionTransition(state, start(operation, "b")).status).toBe("pending");
  });
  it.each(operations)("rejects overlapping %s and every other operation without clearing the pending action", (operation) => {
    const pending = reviewActionTransition({ status: "idle" }, start(operation));
    for (const next of operations) expect(() => reviewActionTransition(pending, start(next, "b"))).toThrow("another action");
    expect(pending.status).toBe("pending");
  });
  it("ignores stale successes and errors from a previous request", () => {
    const old = reviewActionTransition({ status: "idle" }, start("keep"));
    const done = reviewActionTransition(old, { type: "done", requestId: "a" });
    const current = reviewActionTransition(done, start("edit", "b"));
    expect(reviewActionTransition(current, { type: "done", requestId: "a" })).toBe(current);
    expect(reviewActionTransition(current, { type: "failed", requestId: "a", error: new Error("stale") })).toBe(current);
    expect(() => reviewActionTransition(done, { type: "done", requestId: "a" })).toThrow("Illegal review");
  });
  it("refuses non-reviewable changes and unavailable context", () => {
    for (const operation of operations) expect(() => reviewActionTransition({ status: "idle" }, { ...start(operation), permitted: false })).toThrow("not reviewable");
  });
  it.each(["queued", "processing", "failed", "future-status"])("leaves review for %s after any action", (status) => expect(actionLeavesReview(status)).toBe(true));
  it.each(["complete", "manual_remediation"])("keeps review for %s", (status) => expect(actionLeavesReview(status)).toBe(false));
  it("ignores orphaned completion before any action", () => {
    const idle: ReviewAction = { status: "idle" };
    expect(reviewActionTransition(idle, { type: "done", requestId: "unknown" })).toBe(idle);
  });
});
