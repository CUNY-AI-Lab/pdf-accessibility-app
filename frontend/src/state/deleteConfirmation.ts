export type DeleteConfirmation =
  | { status: "closed" | "confirming" | "deleting" }
  | { status: "failed"; error: string };
export type DeleteEvent = { type: "open" | "cancel" | "confirm" | "deleted" } | { type: "failed"; error: string };
export function deleteTransition(state: DeleteConfirmation, event: DeleteEvent): DeleteConfirmation {
  const legal = {
    closed: ["open"], confirming: ["cancel", "confirm"], deleting: ["deleted", "failed"], failed: ["cancel", "confirm"],
  } as const;
  if (!(legal[state.status] as readonly string[]).includes(event.type)) throw new Error(`Illegal deletion transition: ${state.status} -> ${event.type}`);
  switch (event.type) {
    case "open": return { status: "confirming" };
    case "cancel":
    case "deleted": return { status: "closed" };
    case "confirm": return { status: "deleting" };
    case "failed": return { status: "failed", error: event.error };
  }
}
export function canCancelDelete(state: DeleteConfirmation): boolean {
  return state.status === "confirming" || state.status === "failed";
}
