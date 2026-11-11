export type EditorState =
  | { status: "viewing" }
  | { status: "drafting" | "failed"; target: number; draft: string; revision: string }
  | { status: "saving"; target: number; draft: string; revision: string; requestId: string };
export type EditorEvent =
  | { type: "open"; target: number; text: string; revision: string }
  | { type: "type"; text: string }
  | { type: "cancel" }
  | { type: "save"; requestId: string; revision: string }
  | { type: "saved" | "failed"; requestId: string };
export function editorTransition(state: EditorState, event: EditorEvent): EditorState {
  const illegal = () => new Error(`Illegal editor transition: ${state.status} -> ${event.type}`);
  switch (event.type) {
    case "open":
      if (state.status !== "viewing") throw illegal();
      return { status: "drafting", target: event.target, draft: event.text, revision: event.revision };
    case "type":
      if (state.status !== "drafting" && state.status !== "failed") throw illegal();
      return { ...state, status: "drafting", draft: event.text };
    case "cancel":
      if (state.status !== "drafting" && state.status !== "failed") throw illegal();
      return { status: "viewing" };
    case "save":
      if ((state.status !== "drafting" && state.status !== "failed") || !state.draft.trim() || state.revision !== event.revision) throw illegal();
      return { ...state, status: "saving", requestId: event.requestId };
    case "saved":
    case "failed":
      if (state.status !== "saving" || state.requestId !== event.requestId) return state;
      return event.type === "saved" ? { status: "viewing" } : { status: "failed", target: state.target, draft: state.draft, revision: state.revision };
  }
}

export type EditorController = {
  state: EditorState;
  read: () => EditorState;
  send: (event: EditorEvent) => EditorState;
};
