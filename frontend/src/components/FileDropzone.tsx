import { useCallback, useState } from "react";

interface FileDropzoneProps {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
}

export default function FileDropzone({ onFiles, disabled }: FileDropzoneProps) {
  const [isDragging, setIsDragging] = useState(false);

  const handleDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.stopPropagation();
    // Only set isDragging false when actually leaving the dropzone,
    // not when moving over child elements
    if (
      e.currentTarget instanceof Node &&
      !e.currentTarget.contains(e.relatedTarget as Node)
    ) {
      setIsDragging(false);
    }
  }, []);

  const handleDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      e.stopPropagation();
      setIsDragging(false);
      if (disabled) return;

      const files = Array.from(e.dataTransfer.files).filter((f) =>
        f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"),
      );
      if (files.length > 0) onFiles(files);
    },
    [onFiles, disabled],
  );

  const handleClick = useCallback(() => {
    if (disabled) return;
    const input = document.createElement("input");
    input.type = "file";
    input.accept = ".pdf,application/pdf";
    input.multiple = true;
    input.onchange = () => {
      const files = Array.from(input.files || []);
      if (files.length > 0) onFiles(files);
    };
    input.click();
  }, [onFiles, disabled]);

  return (
    <button
      type="button"
      onClick={handleClick}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
      disabled={disabled}
      className={`
        w-full rounded-2xl border-2 border-dashed p-16
        text-center cursor-pointer
        transition-all duration-300 ease-out
        focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent-bright focus-visible:ring-offset-2
        ${
          isDragging
            ? "border-accent-bright bg-accent-glow scale-[1.01] shadow-lifted"
            : "border-ink/12 bg-cream hover:border-accent/40 hover:bg-accent-glow/50 hover:shadow-soft"
        }
        ${disabled ? "opacity-50 cursor-not-allowed" : ""}
      `}
    >
      {/* Upload icon */}
      <div
        className={`
          mx-auto w-16 h-16 rounded-2xl flex items-center justify-center mb-6
          transition-all duration-300
          ${isDragging ? "bg-accent text-white scale-110" : "bg-paper-warm text-ink-muted"}
        `}
      >
        <svg
          width="28"
          height="28"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
          <polyline points="17 8 12 3 7 8" />
          <line x1="12" y1="3" x2="12" y2="15" />
        </svg>
      </div>

      <h3 className="font-display text-xl text-ink mb-2">
        {isDragging ? "Drop your PDFs here" : "Upload PDF documents"}
      </h3>
      <p className="text-sm text-ink-muted max-w-sm mx-auto leading-relaxed">
        Drag and drop PDF files, or click to browse.
        <br />
        <span className="text-xs opacity-70">
          Multiple files supported for batch processing.
        </span>
      </p>
    </button>
  );
}
