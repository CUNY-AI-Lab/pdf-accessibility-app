// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MAX_UPLOAD_BYTES } from "../state/upload";
import UploadPage from "./UploadPage";

let host: HTMLDivElement, root: Root;
const network = vi.fn(async (url: string) => url.endsWith("/health") ? Response.json({ status: "ok" }) : Response.json({ jobs: [{ id: "job-1" }] }));
const uploads = () => network.mock.calls.map(([url]) => url).filter((url) => url.includes("/api/jobs"));

function pdf(name: string, size: number) {
  const file = new File(["%PDF-"], name, { type: "application/pdf", lastModified: 1 });
  Object.defineProperty(file, "size", { value: size });
  return file;
}
function render() {
  act(() => root.render(
    <QueryClientProvider client={new QueryClient()}>
      <MemoryRouter>
        <Routes>
          <Route path="/" element={<UploadPage />} />
          <Route path="/jobs/:id" element={<output data-route="job" />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  ));
}
const dropzone = () => host.querySelector("button")!;
function button(name: string): HTMLButtonElement {
  const found = [...host.querySelectorAll("button")].find((item) => item.textContent?.trim() === name || item.getAttribute("aria-label") === name);
  if (!found) throw new Error(`Button missing: ${name}`);
  return found;
}
/** Chooses files through the drop zone's file picker. */
function pick(...files: File[]) {
  let input: HTMLInputElement | undefined;
  const original = document.createElement.bind(document);
  const spy = vi.spyOn(document, "createElement").mockImplementation((tag, options) => {
    const node = original(tag, options);
    if (tag === "input") input = node as HTMLInputElement;
    return node;
  });
  act(() => dropzone().click());
  spy.mockRestore();
  Object.defineProperty(input!, "files", { value: files });
  act(() => input!.dispatchEvent(new Event("change")));
}
function remove(name: string) {
  const control = button(`Remove ${name}`);
  control.focus();
  act(() => control.click());
}

beforeEach(() => {
  Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true });
  network.mockClear();
  vi.stubGlobal("fetch", network);
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});
afterEach(() => { act(() => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("shows the upload limit on the drop zone", () => {
  render();
  expect(dropzone().textContent).toContain("100 MB");
});

it("refuses an oversized file before any upload request and sends only the files within the limit", async () => {
  render();
  pick(pdf("big.pdf", MAX_UPLOAD_BYTES + 1));
  expect(host.querySelector("[role=alert]")?.textContent).toContain("100 MB");
  expect(button("Start Processing").disabled).toBe(true);
  act(() => button("Start Processing").click());
  expect(network).not.toHaveBeenCalled();

  pick(pdf("small.pdf", 1024));
  expect(button("Start Processing").disabled).toBe(false);
  await act(async () => button("Start Processing").click());
  expect(uploads()).toEqual(["/api/jobs?filename=small.pdf"]);
  expect(host.querySelector("[data-route=job]")).not.toBeNull();
});

it("removes refused and selected files and moves focus to the next control", () => {
  render();
  pick(pdf("a.pdf", 1024), pdf("big.pdf", MAX_UPLOAD_BYTES + 1), pdf("c.pdf", 1024));
  remove("big.pdf");
  expect(host.querySelector("[role=alert]")).toBeNull();
  expect(document.activeElement).toBe(button("Remove c.pdf"));
  remove("c.pdf");
  expect(document.activeElement).toBe(button("Remove a.pdf"));
  remove("a.pdf");
  expect(host.querySelector("[aria-label^=Remove]")).toBeNull();
  expect(document.activeElement).toBe(dropzone());
  expect(network).not.toHaveBeenCalled();
});
