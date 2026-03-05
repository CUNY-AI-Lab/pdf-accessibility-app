import { useState } from "react";
import { Link } from "react-router-dom";
import { useJobs } from "../api/jobs";
import JobCard from "../components/JobCard";


const FILTERS: { label: string; value: string | undefined }[] = [
  { label: "All", value: undefined },
  { label: "Queued", value: "queued" },
  { label: "Processing", value: "processing" },
  { label: "Needs Review", value: "awaiting_review" },
  { label: "Needs Manual Fix", value: "needs_manual_review" },
  { label: "Complete", value: "complete" },
  { label: "Failed", value: "failed" },
];

export default function DashboardPage() {
  const [filter, setFilter] = useState<string | undefined>(undefined);
  const { data, isLoading, error } = useJobs(filter);

  return (
    <div className="animate-fade-in">
      {/* Header */}
      <div className="flex items-end justify-between mb-8">
        <div>
          <h1 className="text-3xl text-ink tracking-tight">Dashboard</h1>
          <p className="text-sm text-ink-muted mt-1">
            {data?.total ?? 0} document{data?.total !== 1 ? "s" : ""} processed
          </p>
        </div>
        <Link
          to="/"
          className="
            inline-flex items-center gap-2 px-4 py-2.5 rounded-xl
            bg-accent text-white text-sm font-medium
            hover:bg-accent/90 shadow-sm hover:shadow-md
            transition-all duration-200 no-underline
          "
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
          </svg>
          Upload
        </Link>
      </div>

      {/* Filters */}
      <div className="flex items-center gap-1 mb-6 overflow-x-auto pb-1">
        {FILTERS.map((f) => (
          <button
            key={f.label}
            type="button"
            onClick={() => setFilter(f.value)}
            className={`
              px-3.5 py-1.5 rounded-lg text-sm font-medium whitespace-nowrap
              transition-all duration-200
              ${
                filter === f.value
                  ? "bg-accent-light text-accent"
                  : "text-ink-muted hover:text-ink hover:bg-paper-warm"
              }
            `}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Content */}
      {isLoading ? (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="rounded-xl bg-cream border border-ink/6 p-5 animate-pulse-soft"
            >
              <div className="h-4 bg-paper-warm rounded w-3/4 mb-3" />
              <div className="h-3 bg-paper-warm rounded w-1/2 mb-4" />
              <div className="h-6 bg-paper-warm rounded-full w-24" />
            </div>
          ))}
        </div>
      ) : error ? (
        <div className="text-center py-16">
          <div className="w-12 h-12 rounded-2xl bg-error-light text-error mx-auto mb-4 flex items-center justify-center">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="12" cy="12" r="10" />
              <line x1="12" y1="8" x2="12" y2="12" />
              <line x1="12" y1="16" x2="12.01" y2="16" />
            </svg>
          </div>
          <p className="text-sm text-error font-medium">Failed to load jobs</p>
          <p className="text-xs text-ink-muted mt-1">
            {error instanceof Error ? error.message : "Unknown error"}
          </p>
        </div>
      ) : data?.jobs.length === 0 ? (
        <div className="text-center py-20">
          <div className="w-16 h-16 rounded-2xl bg-paper-warm text-ink-muted mx-auto mb-5 flex items-center justify-center">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
            </svg>
          </div>
          <h3 className="font-display text-lg text-ink mb-1">No documents yet</h3>
          <p className="text-sm text-ink-muted mb-6">
            Upload your first PDF to get started.
          </p>
          <Link
            to="/"
            className="
              inline-flex items-center gap-2 px-5 py-2.5 rounded-xl
              bg-accent text-white text-sm font-medium
              hover:bg-accent/90 transition-colors no-underline
            "
          >
            Upload a PDF
          </Link>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 stagger">
          {data?.jobs.map((job) => <JobCard key={job.id} job={job} />)}
        </div>
      )}
    </div>
  );
}
