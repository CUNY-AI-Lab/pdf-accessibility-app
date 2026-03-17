import { useToast } from "../hooks/useToast";

export default function Toast() {
  const { toast, dismissToast } = useToast();

  if (!toast) return null;

  return (
    <div
      className="fixed bottom-4 right-4 z-50 flex max-w-sm items-start gap-3 rounded-xl bg-error px-4 py-3 text-white shadow-lifted animate-slide-up"
      role="alert"
    >
      <p className="flex-1 text-sm">{toast.message}</p>
      <button
        onClick={dismissToast}
        className="shrink-0 rounded p-0.5 text-white/70 hover:bg-white/15 hover:text-white"
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
