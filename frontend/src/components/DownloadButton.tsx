import { apiUrl } from "../api/client";

interface DownloadButtonProps {
  jobId: string;
  filename: string;
  type?: "pdf" | "report";
}

export default function DownloadButton({
  jobId,
  filename,
  type = "pdf",
}: DownloadButtonProps) {
  const href =
    type === "pdf"
      ? apiUrl(`/jobs/${jobId}/download`)
      : apiUrl(`/jobs/${jobId}/download/report`);

  const label = type === "pdf" ? "Download Accessible PDF" : "Download Report";

  return (
    <a
      href={href}
      download={type === "pdf" ? `accessible_${filename}` : `report_${filename}.json`}
      className="
        inline-flex items-center gap-2.5 px-5 py-3 rounded-xl
        bg-accent text-white font-medium text-sm
        hover:bg-accent/90 active:bg-accent
        shadow-sm hover:shadow-md
        transition-all duration-200
        no-underline
      "
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
        <polyline points="7 10 12 15 17 10" />
        <line x1="12" y1="15" x2="12" y2="3" />
      </svg>
      {label}
    </a>
  );
}
