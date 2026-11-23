import { readFileSync } from "node:fs";
import { describe, expect, it, vi } from "vitest";
import { emptyUpload, MAX_UPLOAD_BYTES, retryableUploads, runUploadBatch, uploadKey, uploadTransition, type UploadEvent, type UploadState } from "./upload";
import type { Job } from "../types";

const file = (name: string) => new File([name], name, { lastModified: 1 });
function sized(name: string, size: number) {
  const input = file(name);
  Object.defineProperty(input, "size", { value: size });
  return input;
}
const accepted = { id: "accepted" } as Job;
function machine(files = [file("one.pdf"), file("two.pdf")]) {
  let state = uploadTransition(emptyUpload, { type: "add", files });
  return { read: () => state, send: (event: UploadEvent) => (state = uploadTransition(state, event)) };
}

describe("per-file uploads", () => {
  it("retries a partial batch without resubmitting an accepted file", async () => {
    const m = machine();
    const create = vi.fn().mockResolvedValueOnce({ jobs: [accepted] }).mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ jobs: [{ id: "two" }] });
    const first = await runUploadBatch(m.read, m.send, create, "first");
    expect(first.items.map((item) => item.outcome.status)).toEqual(["accepted", "failed"]);
    expect(first.items[0].outcome).toEqual({ status: "accepted", jobs: [accepted] });
    const second = await runUploadBatch(m.read, m.send, create, "second");
    expect(second.items.map((item) => item.outcome.status)).toEqual(["accepted", "accepted"]);
    expect(create.mock.calls.map(([input]) => input.name)).toEqual(["one.pdf", "two.pdf", "two.pdf"]);
  });
  it("rejects double submit and removal while upload is pending", async () => {
    const m = machine();
    let finish!: (value: { jobs: Job[] }) => void;
    const create = vi.fn().mockImplementationOnce(() => new Promise<{ jobs: Job[] }>((resolve) => { finish = resolve; })).mockResolvedValue({ jobs: [accepted] });
    const batch = runUploadBatch(m.read, m.send, create, "first");
    expect(() => m.send({ type: "start", requestId: "second" })).toThrow("Illegal upload");
    await expect(runUploadBatch(m.read, m.send, create, "second")).rejects.toThrow("pending");
    expect(() => m.send({ type: "remove", id: m.read().items[0].id })).toThrow("Illegal upload");
    expect(() => m.send({ type: "add", files: [file("extra.pdf")] })).toThrow();
    finish({ jobs: [accepted] });
    await batch;
    expect(create).toHaveBeenCalledTimes(2);
  });
  it("guards each per-file write and ignores obsolete callbacks", () => {
    const m = machine([file("one.pdf")]);
    const id = m.read().items[0].id;
    m.send({ type: "start", requestId: "a" });
    expect(() => m.send({ type: "accepted", id, requestId: "a", jobs: [] })).toThrow();
    expect(() => m.send({ type: "failed", id, requestId: "a", error: "bad" })).toThrow();
    m.send({ type: "begin", id, requestId: "a" });
    expect(() => m.send({ type: "begin", id, requestId: "a" })).toThrow();
    expect(() => m.send({ type: "finish", requestId: "a" })).toThrow();
    const current = m.read();
    expect(m.send({ type: "failed", id, requestId: "old", error: "stale" })).toBe(current);
    m.send({ type: "accepted", id, requestId: "a", jobs: [accepted] });
    expect(() => m.send({ type: "begin", id, requestId: "a" })).toThrow();
    m.send({ type: "finish", requestId: "a" });
    expect(() => m.send({ type: "start", requestId: "b" })).toThrow();
  });
  it("deduplicates within and across selections and removes a failed error with its item", async () => {
    const input = file("one.pdf");
    const m = machine([input, input]);
    m.send({ type: "add", files: [input] });
    expect(m.read().items).toHaveLength(1);
    await runUploadBatch(m.read, m.send, async () => { throw new Error("bad"); }, "a");
    expect(retryableUploads(m.read())).toHaveLength(1);
    m.send({ type: "remove", id: uploadKey(input) });
    expect(m.read()).toEqual(emptyUpload);
    expect(() => m.send({ type: "remove", id: "missing" })).toThrow();
    expect(() => uploadTransition({ items: [], requestId: null } satisfies UploadState, { type: "start", requestId: "a" })).toThrow();
  });
  it("refuses a file over the Worker limit and never sends or retries it", async () => {
    const m = machine([sized("big.pdf", MAX_UPLOAD_BYTES + 1), sized("edge.pdf", MAX_UPLOAD_BYTES)]);
    expect(m.read().items.map((item) => item.outcome.status)).toEqual(["refused", "selected"]);
    const create = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ jobs: [accepted] });
    await runUploadBatch(m.read, m.send, create, "first");
    await runUploadBatch(m.read, m.send, create, "second");
    expect(create.mock.calls.map(([input]) => input.name)).toEqual(["edge.pdf", "edge.pdf"]);
    expect(retryableUploads(m.read())).toEqual([]);
    m.send({ type: "remove", id: m.read().items[0].id });
    expect(m.read().items.map((item) => item.file.name)).toEqual(["edge.pdf"]);
  });
  it("uses the Worker's upload limit", () => {
    const source = readFileSync(new URL("../../../worker/src/user-api.ts", import.meta.url), "utf8");
    const expression = source.match(/export const MAX_UPLOAD_BYTES = ([^;]+);/)![1];
    const workerLimit = expression.split("*").map((term) => Number(term.trim().replaceAll("_", ""))).reduce((product, term) => product * term, 1);
    expect(MAX_UPLOAD_BYTES).toBe(workerLimit);
  });
});
