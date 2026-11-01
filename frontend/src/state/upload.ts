import type { Job } from "../types";

export type UploadOutcome =
  | { status: "selected" }
  | { status: "uploading"; requestId: string }
  | { status: "accepted"; jobs: Job[] }
  | { status: "failed"; error: string };
export type UploadItem = { id: string; file: File; outcome: UploadOutcome };
export type UploadState = { items: UploadItem[]; requestId: string | null };
export const emptyUpload: UploadState = { items: [], requestId: null };
export type UploadEvent =
  | { type: "add"; files: File[] }
  | { type: "remove"; id: string }
  | { type: "start"; requestId: string }
  | { type: "begin"; id: string; requestId: string }
  | { type: "accepted"; id: string; requestId: string; jobs: Job[] }
  | { type: "failed"; id: string; requestId: string; error: string }
  | { type: "finish"; requestId: string };

export function uploadKey(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}`;
}
export function retryableUploads(state: UploadState): UploadItem[] {
  return state.items.filter(({ outcome }) => outcome.status === "selected" || outcome.status === "failed");
}
export function uploadTransition(state: UploadState, event: UploadEvent): UploadState {
  const illegal = () => new Error(`Illegal upload transition: ${event.type}`);
  if (event.type === "add") {
    if (state.requestId !== null) throw illegal();
    const items = [...state.items];
    for (const file of event.files) {
      const id = uploadKey(file);
      if (!items.some((item) => item.id === id)) items.push({ id, file, outcome: { status: "selected" } });
    }
    return { ...state, items };
  }
  if (event.type === "remove") {
    if (state.requestId !== null || !state.items.some((item) => item.id === event.id)) throw illegal();
    return { ...state, items: state.items.filter((item) => item.id !== event.id) };
  }
  if (event.type === "start") {
    if (state.requestId !== null || retryableUploads(state).length === 0) throw illegal();
    return { ...state, requestId: event.requestId };
  }
  // Obsolete callbacks cannot change a later batch.
  if (event.requestId !== state.requestId) return state;
  if (event.type === "finish") {
    if (state.items.some((item) => item.outcome.status === "uploading")) throw illegal();
    return { ...state, requestId: null };
  }
  const item = state.items.find((item) => item.id === event.id);
  if (!item) throw illegal();
  let outcome: UploadOutcome;
  if (event.type === "begin") {
    if (item.outcome.status !== "selected" && item.outcome.status !== "failed") throw illegal();
    outcome = { status: "uploading", requestId: event.requestId };
  } else {
    if (item.outcome.status !== "uploading" || item.outcome.requestId !== event.requestId) throw illegal();
    outcome = event.type === "accepted" ? { status: "accepted", jobs: event.jobs } : { status: "failed", error: event.error };
  }
  return { ...state, items: state.items.map((entry) => entry.id === item.id ? { ...entry, outcome } : entry) };
}

export async function runUploadBatch(
  read: () => UploadState,
  send: (event: UploadEvent) => UploadState,
  create: (file: File) => Promise<{ jobs: Job[] }>,
  requestId: string,
): Promise<UploadState> {
  if (read().requestId !== null) throw new Error("An upload batch is already pending");
  const pending = retryableUploads(read());
  send({ type: "start", requestId });
  for (const item of pending) {
    send({ type: "begin", id: item.id, requestId });
    try {
      const result = await create(item.file);
      send({ type: "accepted", id: item.id, requestId, jobs: result.jobs });
    } catch (error) {
      send({ type: "failed", id: item.id, requestId, error: error instanceof Error ? error.message : "Upload failed" });
    }
  }
  return send({ type: "finish", requestId });
}
