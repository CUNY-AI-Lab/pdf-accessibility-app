import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useCreateJobs } from "../api/jobs";
import FileDropzone from "../components/FileDropzone";
import { ArrowRightIcon, XIcon } from "../components/Icons";

export default function UploadPage() {
  const navigate = useNavigate();
  const createJobs = useCreateJobs();
  const [selectedFiles, setSelectedFiles] = useState<File[]>([]);

  const handleFiles = (files: File[]) => {
    setSelectedFiles((prev) => [...prev, ...files]);
  };

  const handleUpload = async () => {
    if (selectedFiles.length === 0) return;
    try {
      const result = await createJobs.mutateAsync(selectedFiles);
      setSelectedFiles([]);
      if (result.jobs.length === 1) {
        navigate(`/jobs/${result.jobs[0].id}`);
      } else {
        navigate("/dashboard");
      }
    } catch {
      // error handled by mutation state
    }
  };

  const removeFile = (index: number) => {
    setSelectedFiles((prev) => prev.filter((_, i) => i !== index));
  };

  return (
    <div className="animate-fade-in">
      {/* Hero heading */}
      <div className="text-center mb-10">
        <h1 className="text-4xl md:text-5xl text-ink mb-3 tracking-tight">
          Make PDFs accessible
        </h1>
        <p className="text-lg text-ink-muted max-w-lg mx-auto leading-relaxed">
          Upload a PDF and we'll analyze its structure, generate alt text
          for images, add accessibility tags, and validate compliance.
        </p>
      </div>

      {/* Dropzone */}
      <div className="max-w-2xl mx-auto">
        <FileDropzone
          onFiles={handleFiles}
          disabled={createJobs.isPending}
        />

        {/* Selected files list */}
        {selectedFiles.length > 0 && (
          <div className="mt-6 space-y-2 animate-slide-up">
            <h3 className="text-sm font-semibold text-ink-light mb-3">
              Selected files ({selectedFiles.length})
            </h3>
            {selectedFiles.map((file, i) => (
              <div
                key={`${file.name}-${i}`}
                className="
                  flex items-center justify-between gap-3
                  px-4 py-3 rounded-xl bg-cream border border-ink/6
                "
              >
                <div className="flex items-center gap-3 min-w-0">
                  <div className="w-8 h-8 rounded-lg bg-error-light text-error flex items-center justify-center shrink-0">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                      <polyline points="14 2 14 8 20 8" />
                    </svg>
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-ink truncate">
                      {file.name}
                    </p>
                    <p className="text-xs text-ink-muted">
                      {(file.size / (1024 * 1024)).toFixed(1)} MB
                    </p>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={() => removeFile(i)}
                  className="text-ink-muted hover:text-error transition-colors p-1"
                  aria-label={`Remove ${file.name}`}
                >
                  <XIcon size={14} />
                </button>
              </div>
            ))}

            {/* Upload button */}
            <div className="pt-4">
              <button
                type="button"
                onClick={handleUpload}
                disabled={createJobs.isPending}
                className="
                  w-full py-3.5 rounded-xl
                  bg-accent text-white font-semibold text-sm
                  hover:bg-accent/90 active:bg-accent
                  shadow-sm hover:shadow-md
                  transition-all duration-200
                  disabled:opacity-50 disabled:cursor-not-allowed
                  flex items-center justify-center gap-2
                "
              >
                {createJobs.isPending ? (
                  <>
                    <svg className="animate-spin w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                      <path d="M12 2a10 10 0 0 1 10 10" strokeLinecap="round" />
                    </svg>
                    Uploading...
                  </>
                ) : (
                  <>
                    Start Processing
                    <ArrowRightIcon size={14} />
                  </>
                )}
              </button>
            </div>

            {createJobs.isError && (
              <p className="text-sm text-error bg-error-light rounded-lg px-4 py-3 mt-2">
                {createJobs.error?.message || "Upload failed. Please try again."}
              </p>
            )}
          </div>
        )}

        {/* What to expect */}
        <div className="mt-14 mb-10 text-center">
          <h2 className="text-lg font-display text-ink mb-2">
            What to expect
          </h2>
          <p className="text-sm text-ink-muted leading-relaxed max-w-md mx-auto">
            Processing typically takes 1&ndash;3 minutes depending on document
            length. You'll get an accessible PDF with proper structure tags,
            alt text for images, and a compliance report.
          </p>
        </div>

        {/* Pipeline overview */}
        <h3 className="text-sm font-semibold text-ink-muted text-center mb-4 tracking-wide uppercase">
          Our 6-step process
        </h3>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-4 stagger">
          {[
            { icon: "M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2", label: "Classify", desc: "Detect document type" },
            { icon: "M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z", label: "OCR", desc: "Extract text from scans" },
            { icon: "M3 3h18v18H3zM3 9h18M9 3v18", label: "Structure", desc: "Analyze layout" },
            { icon: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z", label: "Alt Text", desc: "Describe images" },
            { icon: "M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z", label: "Tag", desc: "Add PDF/UA tags" },
            { icon: "M9 12l2 2 4-4m6 2a9 9 0 1 1-18 0 9 9 0 0 1 18 0z", label: "Validate", desc: "Check compliance" },
          ].map((step) => (
            <div
              key={step.label}
              className="
                px-4 py-4 rounded-xl bg-cream border border-ink/5
                text-center
              "
            >
              <div className="w-9 h-9 rounded-xl bg-paper-warm text-ink-muted mx-auto mb-2 flex items-center justify-center">
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                  <path d={step.icon} />
                </svg>
              </div>
              <p className="text-sm font-semibold text-ink">{step.label}</p>
              <p className="text-xs text-ink-muted mt-0.5">{step.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
