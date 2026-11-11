import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useEffect,
} from "react";
import type { ReactNode } from "react";
import { useMachine } from "./useMachine";
import { toastRegistry, toastTransition, type ToastState } from "../state/toast";

interface Toast {
  message: string;
  id: number;
}

interface ToastContextValue {
  toast: Toast | null;
  showToast: (message: string) => void;
  dismissToast: () => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

let nextId = 0;

const registry = toastRegistry();
export function showToastGlobal(message: string) {
  registry.show(message);
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const { state, send } = useMachine<ToastState, Parameters<typeof toastTransition>[1]>({ status: "hidden" }, toastTransition);
  const dismissToast = useCallback(() => { send({ type: "dismiss" }); }, [send]);
  const showToast = useCallback((message: string) => {
    send({ type: "show", id: ++nextId, message });
  }, [send]);
  useEffect(() => registry.register(showToast), [showToast]);
  useEffect(() => {
    if (state.status !== "visible") return;
    const timer = setTimeout(() => send({ type: "expire", id: state.id }), 5000);
    return () => clearTimeout(timer);
  }, [state, send]);
  const toast = state.status === "visible" ? { id: state.id, message: state.message } : null;

  return createElement(
    ToastContext.Provider,
    { value: { toast, showToast, dismissToast } },
    children,
  );
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext);
  if (!ctx) {
    throw new Error("useToast must be used within a ToastProvider");
  }
  return ctx;
}
