import { useEffect, useState } from "react";
import { useToast } from "../hooks/useToast";

export default function Toast() {
  const { toast, dismissToast } = useToast();
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (toast) {
      // Trigger enter animation on next frame
      requestAnimationFrame(() => setVisible(true));
    } else {
      setVisible(false);
    }
  }, [toast]);

  if (!toast) return null;

  return (
    <div
      className={`fixed bottom-4 right-4 z-50 flex max-w-sm items-start gap-3 rounded-lg bg-red-600 px-4 py-3 text-white shadow-lg transition-all duration-300 ${
        visible ? "translate-y-0 opacity-100" : "translate-y-2 opacity-0"
      }`}
      role="alert"
    >
      <p className="flex-1 text-sm">{toast.message}</p>
      <button
        onClick={dismissToast}
        className="shrink-0 rounded p-0.5 text-red-100 hover:bg-red-700 hover:text-white"
        aria-label="Dismiss"
      >
        <svg
          className="h-4 w-4"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M6 18L18 6M6 6l12 12"
          />
        </svg>
      </button>
    </div>
  );
}
