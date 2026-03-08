import {
  createContext,
  createElement,
  useCallback,
  useContext,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";

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

// Module-level callback so code outside React (e.g. QueryClient) can show toasts.
let globalShowToast: ((message: string) => void) | null = null;

export function showToastGlobal(message: string) {
  if (globalShowToast) {
    globalShowToast(message);
  } else {
    console.error("[Toast]", message);
  }
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<Toast | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout>>(null);

  const dismissToast = useCallback(() => {
    setToast(null);
  }, []);

  const showToast = useCallback(
    (message: string) => {
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
      const id = ++nextId;
      setToast({ message, id });
      timerRef.current = setTimeout(() => {
        setToast((current) => (current?.id === id ? null : current));
      }, 5000);
    },
    [],
  );

  // Register the React-bound showToast as the global handler.
  globalShowToast = showToast;

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
