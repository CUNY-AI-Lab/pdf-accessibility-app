import { describe, expect, it } from "vitest";
import { guardArtifactTransition, guardRetentionTransition, guardUploadTransition } from "../src/artifacts";
import { credentialEligibility } from "../src/credential";
import { applyStepProgress, guardStepTransition, resetSteps, validateSteps } from "../src/steps";
import { finishedJob } from "./helpers";

const steps = () => finishedJob("complete").steps;

describe("artifact guard", () => {
  it.each(["staging", "sealed", "published", "abandoned", "legacy"])("checks all targets from %s", (state) => {
    for (const target of ["staging", "sealed", "published", "abandoned"] as const) {
      const allowed = (state === "staging" && ["sealed", "abandoned"].includes(target)) || (state === "sealed" && ["published", "abandoned"].includes(target));
      if (allowed) expect(() => guardArtifactTransition(state, target)).not.toThrow();
      else expect(() => guardArtifactTransition(state, target)).toThrow("Illegal artifact transition");
    }
  });
});

describe("retention guard", () => {
  it.each(["retained", "deletion_requested", "deleting", "delete_failed", "deleted", "legacy"])("checks all targets from %s", (state) => {
    for (const target of ["retained", "deletion_requested", "deleting", "delete_failed", "deleted"] as const) {
      const allowed = (state === "retained" && target === "deletion_requested") || (["deletion_requested", "delete_failed"].includes(state) && target === "deleting") || (state === "deleting" && ["deleted", "delete_failed", "deleting"].includes(target));
      if (allowed) expect(() => guardRetentionTransition(state, target)).not.toThrow();
      else expect(() => guardRetentionTransition(state, target)).toThrow("Illegal retention transition");
    }
  });
});

describe("upload admission guard", () => {
  it.each(["receiving", "stored", "admitted", "abandoned", "legacy"])("checks all targets from %s", (state) => {
    for (const target of ["receiving", "stored", "admitted", "abandoned"] as const) {
      const allowed = (state === "receiving" && ["stored", "abandoned"].includes(target)) || (state === "stored" && ["admitted", "abandoned"].includes(target));
      if (allowed) expect(() => guardUploadTransition(state, target)).not.toThrow();
      else expect(() => guardUploadTransition(state, target)).toThrow("Illegal upload admission transition");
    }
  });
});

describe("step guard", () => {
  it.each(["pending", "running", "complete", "skipped", "failed", "legacy"])("checks all targets from %s", (state) => {
    for (const target of ["pending", "running", "complete", "skipped", "failed", "invented"]) {
      const allowed = (state === "pending" && target !== "invented") || (state === "running" && ["running", "complete", "skipped", "failed"].includes(target)) || (["complete", "skipped", "failed"].includes(state) && target === state);
      if (allowed) expect(() => guardStepTransition(state, target)).not.toThrow();
      else expect(() => guardStepTransition(state, target)).toThrow("Illegal step transition");
    }
  });

  it("resets review steps without resetting earlier results", () => {
    const reset = resetSteps(steps(), true);
    expect(reset.slice(0, 4)).toEqual(steps().slice(0, 4));
    expect(reset.slice(4).every((step) => step.status === "pending" && step.result === null && step.completed_at === null)).toBe(true);
    const running = applyStepProgress(reset, { step: "tagging", status: "running" }, 100);
    expect(running[4]?.started_at).toBe(new Date(100).toISOString());
    expect(running[4]?.error).toBeNull();
    expect(() => applyStepProgress(steps(), { step: "tagging", status: "running" }, 100)).toThrow();
  });

  it("requires seven unique known steps and tolerates the deployed review outcome event", () => {
    expect(() => validateSteps(steps())).not.toThrow();
    expect(() => validateSteps(steps().slice(1))).toThrow();
    const duplicate = steps();
    duplicate[0] = { ...duplicate[0]!, step_name: "ocr" };
    expect(() => validateSteps(duplicate)).toThrow();
    expect(applyStepProgress(steps(), { step: "review", status: "manual_remediation" }, 100)).toEqual(steps());
    expect(() => applyStepProgress(steps(), { step: "invented", status: "running" }, 100)).toThrow();
  });
});

describe("credential eligibility", () => {
  it.each([
    [60 * 60_000, "queue_eligible"], [60 * 60_000 - 1, "claim_eligible"],
    [15 * 60_000, "claim_eligible"], [15 * 60_000 - 1, "relay_only"],
    [10 * 60_000 + 1, "relay_only"], [10 * 60_000, "expired"], [0, "expired"],
  ])("checks exact remaining lifetime %s", (remaining, expected) => {
    expect(credentialEligibility("process", "token", Number(remaining) + 100, 100)).toBe(expected);
  });
  it("rejects incomplete pairs and exempts sync", () => {
    expect(credentialEligibility("process", null, 999999, 0)).toBe("missing");
    expect(credentialEligibility("review", "token", null, 0)).toBe("missing");
    expect(credentialEligibility("sync", null, null, 0)).toBe("not_required");
  });
});
