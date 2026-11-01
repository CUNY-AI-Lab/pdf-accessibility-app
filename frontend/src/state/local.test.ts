import { describe, expect, it, vi } from "vitest";
import { canCancelDelete, deleteTransition, type DeleteConfirmation, type DeleteEvent } from "./deleteConfirmation";
import { editorTransition, type EditorState } from "./editor";
import { imageTransition, initialImage } from "./image";
import { toastRegistry, toastTransition, type ToastState } from "./toast";
import { acceptPickedFiles, dropzoneMode, dropzoneTransition } from "./dropzone";
import { responseOutcome } from "./request";

const draft: EditorState = { status: "drafting", target: 1, draft: "description", revision: "1" };
describe("alt text editor", () => {
  it("opens, types, cancels, saves and closes only its own save", () => {
    const opened = editorTransition({ status: "viewing" }, { type: "open", target: 1, text: "old", revision: "1" });
    const typed = editorTransition(opened, { type: "type", text: "new" });
    expect(typed).toEqual({ status: "drafting", target: 1, draft: "new", revision: "1" });
    expect(editorTransition(typed, { type: "cancel" })).toEqual({ status: "viewing" });
    const saving = editorTransition(typed, { type: "save", requestId: "a", revision: "1" });
    expect(editorTransition(saving, { type: "saved", requestId: "old" })).toBe(saving);
    expect(editorTransition(saving, { type: "saved", requestId: "a" })).toEqual({ status: "viewing" });
  });
  it("freezes typing and cancellation while saving and retains text after failure", () => {
    const saving = editorTransition(draft, { type: "save", requestId: "a", revision: "1" });
    expect(() => editorTransition(saving, { type: "type", text: "typed during save" })).toThrow();
    expect(() => editorTransition(saving, { type: "cancel" })).toThrow();
    expect(() => editorTransition(saving, { type: "open", target: 1, text: "another", revision: "1" })).toThrow();
    expect(() => editorTransition(saving, { type: "save", requestId: "b", revision: "1" })).toThrow();
    const failed = editorTransition(saving, { type: "failed", requestId: "a" });
    expect(failed).toEqual({ ...draft, status: "failed" });
    expect(editorTransition(failed, { type: "type", text: "corrected" })).toEqual({ ...draft, draft: "corrected" });
    expect(editorTransition(failed, { type: "cancel" }).status).toBe("viewing");
    expect(editorTransition(failed, { type: "save", requestId: "b", revision: "1" }).status).toBe("saving");
    expect(editorTransition(failed, { type: "saved", requestId: "a" })).toBe(failed);
  });
  it("refuses stale revision and empty saves without losing the draft", () => {
    expect(() => editorTransition(draft, { type: "save", requestId: "a", revision: "2" })).toThrow();
    expect(draft).toMatchObject({ draft: "description", revision: "1" });
    expect(() => editorTransition({ ...draft, draft: " " }, { type: "save", requestId: "a", revision: "1" })).toThrow();
    for (const event of [{ type: "cancel" }, { type: "type", text: "x" }, { type: "save", requestId: "a", revision: "1" }] as const) expect(() => editorTransition({ status: "viewing" }, event)).toThrow();
  });
});

describe("deletion guard", () => {
  const states: DeleteConfirmation[] = [{ status: "closed" }, { status: "confirming" }, { status: "deleting" }, { status: "failed", error: "offline" }];
  const events: DeleteEvent[] = [{ type: "open" }, { type: "cancel" }, { type: "confirm" }, { type: "deleted" }, { type: "failed", error: "offline" }];
  const allowed: Record<string, Record<string, string>> = { closed: { open: "confirming" }, confirming: { cancel: "closed", confirm: "deleting" }, deleting: { deleted: "closed", failed: "failed" }, failed: { cancel: "closed", confirm: "deleting" } };
  for (const state of states) for (const event of events) {
    it(`${state.status} -> ${event.type} is guarded`, () => {
      const expected = allowed[state.status][event.type];
      if (expected) expect(deleteTransition(state, event).status).toBe(expected);
      else expect(() => deleteTransition(state, event)).toThrow("Illegal deletion");
    });
  }
  it("prevents Escape cancellation during deletion but permits cancel after rejection", () => {
    expect(canCancelDelete({ status: "deleting" })).toBe(false);
    expect(canCancelDelete({ status: "failed", error: "offline" })).toBe(true);
    expect(deleteTransition({ status: "failed", error: "offline" }, { type: "confirm" })).toEqual({ status: "deleting" });
  });
});

describe("image events", () => {
  it("distinguishes missing source and guards retry", () => {
    expect(initialImage().status).toBe("missing");
    expect(() => imageTransition(initialImage(), { type: "retry" })).toThrow();
    expect(() => imageTransition(initialImage("a"), { type: "retry" })).toThrow();
  });
  it("retries failed source, rejects stale events, and starts fresh for a replacement source", () => {
    const a = imageTransition(initialImage("a"), { type: "failed", source: "a", attempt: 0 });
    const retry = imageTransition(a, { type: "retry" });
    expect(retry).toMatchObject({ status: "loading", attempt: 1 });
    expect(imageTransition(retry, { type: "failed", source: "a", attempt: 0 })).toBe(retry);
    const b = initialImage("b");
    expect(imageTransition(b, { type: "loaded", source: "a", attempt: 0 })).toBe(b);
    const loaded = imageTransition(b, { type: "loaded", source: "b", attempt: 0 });
    expect(loaded.status).toBe("loaded");
    expect(() => imageTransition(loaded, { type: "failed", source: "b", attempt: 0 })).toThrow();
  });
});

describe("toast lifetime", () => {
  it("replacement and dismiss cannot let an old timer hide a newer toast", () => {
    let state: ToastState = { status: "hidden" };
    state = toastTransition(state, { type: "show", id: 1, message: "one" });
    state = toastTransition(state, { type: "show", id: 2, message: "two" });
    expect(toastTransition(state, { type: "expire", id: 1 })).toBe(state);
    state = toastTransition(state, { type: "dismiss" });
    expect(state.status).toBe("hidden");
    state = toastTransition(state, { type: "show", id: 3, message: "three" });
    expect(toastTransition(state, { type: "expire", id: 2 })).toBe(state);
    expect(toastTransition(state, { type: "expire", id: 3 }).status).toBe("hidden");
  });
  it("unregisters only its own callback on provider teardown", () => {
    const registry = toastRegistry();
    const first = vi.fn(), second = vi.fn();
    const cleanFirst = registry.register(first);
    const cleanSecond = registry.register(second);
    cleanFirst();
    registry.show("current");
    expect(second).toHaveBeenCalledWith("current");
    expect(first).not.toHaveBeenCalled();
    cleanSecond();
    const fallback = vi.spyOn(console, "error").mockImplementation(() => {});
    registry.show("unmounted");
    expect(second).toHaveBeenCalledTimes(1);
    expect(fallback).toHaveBeenCalled();
    fallback.mockRestore();
  });
});

describe("request and dropzone modes", () => {
  it("only accepts a Doorway launch redirect on authentication failure", () => {
    expect(responseOutcome(200, "", null).status).toBe("success");
    expect(responseOutcome(401, "ended", "/launch/pdf")).toEqual({ status: "redirect", launch: "/launch/pdf" });
    for (const [status, launch] of [[403, "/launch/pdf"], [401, "https://other.test/launch/pdf"], [401, null]] as const) expect(responseOutcome(status, "error", launch)).toEqual({ status: "failed", detail: "error" });
  });
  it("projects disabled state over dragging and rechecks a late picker result", () => {
    expect(dropzoneTransition("idle", "enter")).toBe("dragging");
    expect(dropzoneTransition("dragging", "leave")).toBe("idle");
    expect(dropzoneTransition("dragging", "drop")).toBe("idle");
    expect(dropzoneMode("dragging", true)).toBe("disabled");
    const picked = [new File([""], "a.pdf"), new File([""], "bad.txt")];
    const onFiles = vi.fn();
    acceptPickedFiles(picked, true, onFiles);
    expect(onFiles).not.toHaveBeenCalled();
    acceptPickedFiles(picked, false, onFiles);
    expect(onFiles).toHaveBeenCalledWith([picked[0]]);
  });
});

describe("filter and disclosure", () => {
  it("permits all known filters and rejects an unrecognized write", async () => {
    const { filterTransition } = await import("./modes");
    const { JOB_STATUSES } = await import("../types");
    const choices = [undefined, ...JOB_STATUSES];
    for (const previous of choices) for (const next of choices) expect(filterTransition(previous, next)).toBe(next);
    expect(() => filterTransition(undefined, "legacy" as typeof choices[number])).toThrow("Unknown job filter");
  });
  it("toggles details in either direction", async () => {
    const { disclosureTransition } = await import("./modes");
    expect(disclosureTransition("collapsed", "toggle")).toBe("expanded");
    expect(disclosureTransition("expanded", "toggle")).toBe("collapsed");
  });
});
