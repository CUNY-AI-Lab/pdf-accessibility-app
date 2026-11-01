export type ImageState = { status: "missing" | "loading" | "loaded" | "failed"; source: string | null; attempt: number };
export type ImageEvent = { type: "loaded" | "failed"; source: string; attempt: number } | { type: "retry" };
export function initialImage(source?: string | null): ImageState {
  return { status: source ? "loading" : "missing", source: source ?? null, attempt: 0 };
}
export function imageTransition(state: ImageState, event: ImageEvent): ImageState {
  if (event.type === "retry") {
    if (state.status !== "failed") throw new Error(`Cannot retry image in ${state.status}`);
    return { ...state, status: "loading", attempt: state.attempt + 1 };
  }
  if (event.source !== state.source || event.attempt !== state.attempt) return state;
  if (state.status !== "loading") throw new Error(`Illegal image transition: ${state.status} -> ${event.type}`);
  return { ...state, status: event.type };
}
