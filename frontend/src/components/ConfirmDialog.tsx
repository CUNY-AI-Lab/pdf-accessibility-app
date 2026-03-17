import { useEffect, useRef } from "react";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  confirmLabel?: string;
  cancelLabel?: string;
  confirmPending?: boolean;
  errorMessage?: string | null;
}

export default function ConfirmDialog({
  open,
  title,
  message,
  onConfirm,
  onCancel,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  confirmPending = false,
  errorMessage = null,
}: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (open) {
      if (!dialog.open) {
        dialog.showModal();
      }
    } else {
      if (dialog.open) {
        dialog.close();
      }
    }
  }, [open]);

  const handleClose = () => {
    // The close event fires when the dialog is dismissed via Escape or .close()
    onCancel();
  };

  return (
    <dialog
      ref={dialogRef}
      onClose={handleClose}
      className="
        rounded-xl border border-ink/10 bg-cream p-0 shadow-xl
        backdrop:bg-ink/30 backdrop:backdrop-blur-sm
        max-w-sm w-full
      "
    >
      <div className="p-6">
        <h2 className="font-display text-lg text-ink mb-2">{title}</h2>
        <p className="text-sm text-ink-muted">{message}</p>
        {errorMessage && (
          <p className="mt-4 rounded-lg border border-error/20 bg-error-light px-3 py-2 text-sm text-error">
            {errorMessage}
          </p>
        )}
      </div>
      <div className="flex justify-end gap-3 px-6 pb-6">
        <button
          type="button"
          onClick={onCancel}
          disabled={confirmPending}
          className="
            px-4 py-2 rounded-lg text-sm font-medium
            bg-paper-warm text-ink-muted
            hover:bg-paper-warm/80 transition-colors disabled:cursor-not-allowed disabled:opacity-60
          "
        >
          {cancelLabel}
        </button>
        <button
          type="button"
          onClick={onConfirm}
          disabled={confirmPending}
          className="
            px-4 py-2 rounded-lg text-sm font-medium
            bg-error text-white
            hover:bg-error/90 transition-colors disabled:cursor-not-allowed disabled:opacity-70
          "
        >
          {confirmPending ? `${confirmLabel}...` : confirmLabel}
        </button>
      </div>
    </dialog>
  );
}
