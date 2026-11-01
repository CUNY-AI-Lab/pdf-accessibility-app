export type ReviewOperation = "keep" | "undo" | "revise" | "edit" | "resolve";
export type ActionRequest = { requestId: string; operation: ReviewOperation; target: number; revision: string };
export type ReviewAction =
  | { status: "idle" }
  | ({ status: "pending" | "done" } & ActionRequest)
  | ({ status: "failed"; error: Error } & ActionRequest);
export type ReviewActionEvent =
  | ({ type: "start"; permitted: boolean } & ActionRequest)
  | { type: "done"; requestId: string }
  | { type: "failed"; requestId: string; error: Error };

export function reviewActionTransition(state: ReviewAction, event: ReviewActionEvent): ReviewAction {
  if (event.type === "start") {
    if (state.status === "pending" || !event.permitted) throw new Error("Review action is not available while another action is pending or output is not reviewable");
    return { status: "pending", requestId: event.requestId, operation: event.operation, target: event.target, revision: event.revision };
  }
  if (state.status === "idle" || event.requestId !== state.requestId) return state;
  if (state.status !== "pending") throw new Error(`Illegal review action transition: ${state.status} -> ${event.type}`);
  return event.type === "done" ? { ...state, status: "done" } : { ...state, status: "failed", error: event.error };
}

export function actionLeavesReview(status: string): boolean {
  return status !== "complete" && status !== "manual_remediation";
}
