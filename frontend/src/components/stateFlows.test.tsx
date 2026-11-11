// @vitest-environment jsdom
import { act, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppliedChange, Job } from "../types";
import { useMachine } from "../hooks/useMachine";
import { editorTransition, type EditorState } from "../state/editor";
import { showToastGlobal, ToastProvider } from "../hooks/useToast";
import Toast from "./Toast";
import AppliedChangeCard from "./AppliedChangeCard";
import PreviewImage from "./PreviewImage";
import ErrorBoundary from "./ErrorBoundary";
import JobCard from "./JobCard";
import JobDetailPage from "../pages/JobDetailPage";
import OutcomeHero from "./OutcomeHero";
import FileDropzone from "./FileDropzone";

const mocks = vi.hoisted(() => ({ deleteJob: vi.fn() }));
vi.mock("../api/jobs", () => ({
  useDeleteJob: () => ({ mutateAsync: mocks.deleteJob }),
  useJob: () => ({ data: { id: "job", status: "failed", original_filename: "a.pdf", steps: [] }, isLoading: false, error: null }),
  useReviewContext: () => ({ status: "not_applicable" }),
}));

let host: HTMLDivElement;
let root: Root;
function render(node: ReactNode) { act(() => root.render(node)); }
function button(name: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((item) => item.textContent?.trim() === name || item.getAttribute("aria-label") === name);
  if (!found) throw new Error(`Button missing: ${name}`);
  return found;
}
function click(name: string) { act(() => button(name).click()); }
function deferred<T>() {
  let resolve!: (value: T) => void, reject!: (error: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
  // jsdom doesn't implement the dialog presentation methods. The cancel event
  // itself is dispatched as a native cancelable event below.
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new Event("close")); };
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.useRealTimers(); });

const sampleJob: Job = { id: "job", filename: "a.pdf", original_filename: "a.pdf", status: "failed", created_at: "2026-01-01", updated_at: "2026-01-01", steps: [] };

describe("delete entry points", () => {
  it.each(["card", "detail"])("keeps %s dialog visible after Escape during pending deletion and rejection; retry succeeds", async (entry) => {
    const first = deferred<void>();
    const second = deferred<void>();
    mocks.deleteJob.mockReset().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    render(<MemoryRouter>{entry === "card" ? <JobCard job={sampleJob} /> : <JobDetailPage />}</MemoryRouter>);
    click(entry === "card" ? "Delete a.pdf" : "Delete Job");
    let dialog = host.querySelector("dialog")!;
    expect(dialog.open).toBe(true);
    click("Delete");
    // Two confirms in one render interval also reach only one request.
    click("Delete...");
    expect(mocks.deleteJob).toHaveBeenCalledTimes(1);
    const escape = new Event("cancel", { cancelable: true });
    act(() => dialog.dispatchEvent(escape));
    expect(escape.defaultPrevented).toBe(true);
    expect(dialog.open).toBe(true);
    await act(async () => { first.reject(new Error("offline")); await first.promise.catch(() => {}); });
    dialog = host.querySelector("dialog")!;
    expect(dialog.open).toBe(true);
    expect(dialog.textContent).toContain("offline");
    expect(button("Delete").disabled).toBe(false);
    click("Delete");
    await act(async () => { second.resolve(); await second.promise; });
    expect(host.querySelector("dialog")?.open ?? false).toBe(false);
  });
});

const change: AppliedChange = { id: 1, change_type: "figure_semantics", title: "Figure", detail: "", importance: "low", review_status: "pending_review", reviewable: true, metadata: {}, before: {}, after: { generated_text: "Existing description" } };
function EditorHarness({ onEdit, revision = "1", reviewable = true }: { onEdit: () => Promise<void>; revision?: string; reviewable?: boolean }) {
  const editor = useMachine<EditorState, Parameters<typeof editorTransition>[1]>({ status: "viewing" }, editorTransition);
  return <AppliedChangeCard jobId="job" change={{ ...change, reviewable }} revision={revision} editor={editor} disabled={false} keeping={false} undoing={false} editing={false} revising={false} onKeep={() => {}} onUndo={() => {}} onRevise={() => {}} onEdit={onEdit} />;
}

describe("editing", () => {
  it("blocks alternate actions during a draft, freezes submitted text, and retains a rejected save", async () => {
    const saving = deferred<void>();
    const onEdit = vi.fn(() => saving.promise);
    render(<EditorHarness onEdit={onEdit} />);
    click("Edit");
    expect(button("Keep").disabled).toBe(true);
    expect(button("Undo").disabled).toBe(true);
    click("Save");
    expect(host.querySelector("textarea")!.disabled).toBe(true);
    expect(button("Cancel").disabled).toBe(true);
    await act(async () => { saving.reject(new Error("refused")); await saving.promise.catch(() => {}); });
    expect(host.querySelector("textarea")!.value).toBe("Existing description");
    expect(host.querySelector("textarea")!.disabled).toBe(false);
    expect(button("Save").disabled).toBe(false);
    click("Cancel");
    expect(button("Keep").disabled).toBe(false);
  });
  it("retains a draft on record refresh and refuses to save it against another revision", () => {
    const save = vi.fn(async () => {});
    render(<EditorHarness onEdit={save} />);
    click("Edit");
    render(<EditorHarness onEdit={save} revision="2" />);
    expect(host.querySelector("textarea")!.value).toBe("Existing description");
    expect(button("Save").disabled).toBe(true);
    expect(host.querySelector("[role=alert]")).not.toBeNull();
    expect(save).not.toHaveBeenCalled();
  });
  it("honors reviewable false", () => {
    render(<EditorHarness onEdit={async () => {}} reviewable={false} />);
    for (const name of ["Keep", "Undo", "Edit"]) expect(button(name).disabled).toBe(true);
  });
});

describe("image, toast and boundary lifetimes", () => {
  it("renders fallback after failure, retries the same source, and shows a replacement source", () => {
    render(<PreviewImage src="/a.png" alt="Figure" title="Preview" />);
    act(() => host.querySelector("img")!.dispatchEvent(new Event("error")));
    expect(host.querySelector("img")).toBeNull();
    click("Retry preview");
    expect(host.querySelector("img")?.getAttribute("src")).toBe("/a.png");
    act(() => host.querySelector("img")!.dispatchEvent(new Event("error")));
    render(<PreviewImage src="/b.png" alt="Figure" title="Preview" />);
    act(() => host.querySelector("img")!.dispatchEvent(new Event("load")));
    expect(host.querySelector("img")?.getAttribute("src")).toBe("/b.png");
  });
  it("replaces a toast, dismisses it, and clears the timer and registration on unmount", () => {
    vi.useFakeTimers();
    render(<ToastProvider><Toast /></ToastProvider>);
    act(() => showToastGlobal("first"));
    act(() => vi.advanceTimersByTime(4000));
    act(() => showToastGlobal("second"));
    act(() => vi.advanceTimersByTime(1000));
    expect(host.querySelector("[role=alert]")?.textContent).toContain("second");
    click("Dismiss");
    expect(host.querySelector("[role=alert]")).toBeNull();
    act(() => showToastGlobal("third"));
    render(null);
    expect(vi.getTimerCount()).toBe(0);
    const fallback = vi.spyOn(console, "error").mockImplementation(() => {});
    act(() => showToastGlobal("after unmount"));
    expect(fallback).toHaveBeenCalled();
  });
  it("catches a render crash and reconstructs clean state on remount", () => {
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    function Crash(): ReactNode { throw new Error("broken"); }
    render(<ErrorBoundary><Crash /></ErrorBoundary>);
    expect(button("Reload page")).toBeDefined();
    expect(log).toHaveBeenCalled();
    render(<ErrorBoundary key="new">Recovered</ErrorBoundary>);
    expect(host.textContent).toBe("Recovered");
  });
  it("rechecks disabled state when an already-open file picker returns", () => {
    let input: HTMLInputElement | undefined;
    const original = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag, options) => {
      const node = original(tag, options);
      if (tag === "input") input = node as HTMLInputElement;
      return node;
    });
    const files = vi.fn();
    render(<FileDropzone onFiles={files} />);
    act(() => host.querySelector("button")!.click());
    render(<FileDropzone onFiles={files} disabled />);
    Object.defineProperty(input!, "files", { value: [new File(["pdf"], "a.pdf")] });
    act(() => input!.dispatchEvent(new Event("change")));
    expect(files).not.toHaveBeenCalled();
  });
});

it.each(["ready", "loading", "unavailable"] as const)("does not treat complete/noncompliant output as passed when context is %s", (context) => {
  render(<MemoryRouter><OutcomeHero jobId="job" filename="a.pdf" status="complete" validationCompliant={false} reviewContextStatus={context} /></MemoryRouter>);
  expect(host.querySelectorAll("a[download]")).toHaveLength(2);
});
