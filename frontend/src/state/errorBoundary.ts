export type BoundaryState = { status: "ready" | "crashed" };
export function boundaryTransition(_state: BoundaryState, event: "crash"): BoundaryState {
  if (event !== "crash") throw new Error("Illegal error boundary transition");
  return { status: "crashed" };
}
