export type ToastState = { status: "hidden" } | { status: "visible"; id: number; message: string };
export type ToastEvent = { type: "show"; id: number; message: string } | { type: "dismiss" } | { type: "expire"; id: number };
export function toastTransition(state: ToastState, event: ToastEvent): ToastState {
  if (event.type === "show") return { status: "visible", id: event.id, message: event.message };
  if (event.type === "expire" && (state.status !== "visible" || state.id !== event.id)) return state;
  return { status: "hidden" };
}

// Cleanup may only unregister the callback it installed (including StrictMode).
export function toastRegistry() {
  let callback: ((message: string) => void) | null = null;
  return {
    register(next: (message: string) => void) {
      callback = next;
      return () => { if (callback === next) callback = null; };
    },
    show(message: string) {
      if (callback) callback(message);
      else console.error("[Toast]", message);
    },
  };
}
