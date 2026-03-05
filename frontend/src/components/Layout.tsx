import { Link, NavLink, Outlet } from "react-router-dom";

export default function Layout() {
  return (
    <div className="min-h-screen flex flex-col">
      {/* Header */}
      <header className="border-b border-ink/8 bg-cream/80 backdrop-blur-sm sticky top-0 z-50">
        <div className="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
          <Link to="/" className="flex items-center gap-3 no-underline group">
            {/* Logo mark */}
            <div className="w-9 h-9 rounded-lg bg-accent flex items-center justify-center shadow-sm group-hover:shadow-glow transition-shadow duration-300">
              <svg
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="white"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
                <circle cx="12" cy="15" r="3" />
                <path d="M12 12v0" />
              </svg>
            </div>
            <div>
              <span className="font-display text-lg font-semibold tracking-tight text-ink">
                PDF Accessibility
              </span>
              <span className="hidden sm:inline ml-2 text-xs font-mono text-ink-muted bg-paper-warm px-2 py-0.5 rounded-full">
                CUNY AI Lab
              </span>
            </div>
          </Link>

          <nav className="flex items-center gap-1">
            <NavLink
              to="/"
              end
              className={({ isActive }) =>
                `px-3 py-1.5 rounded-lg text-sm font-medium transition-all duration-200 no-underline ${
                  isActive
                    ? "bg-accent-light text-accent"
                    : "text-ink-muted hover:text-ink hover:bg-paper-warm"
                }`
              }
            >
              Upload
            </NavLink>
            <NavLink
              to="/dashboard"
              className={({ isActive }) =>
                `px-3 py-1.5 rounded-lg text-sm font-medium transition-all duration-200 no-underline ${
                  isActive
                    ? "bg-accent-light text-accent"
                    : "text-ink-muted hover:text-ink hover:bg-paper-warm"
                }`
              }
            >
              Dashboard
            </NavLink>
          </nav>
        </div>
      </header>

      {/* Main content */}
      <main className="flex-1">
        <div className="max-w-6xl mx-auto px-6 py-10">
          <Outlet />
        </div>
      </main>

      {/* Footer */}
      <footer className="border-t border-ink/5 py-6">
        <div className="max-w-6xl mx-auto px-6 flex items-center justify-between text-xs text-ink-muted">
          <span>PDF Accessibility Tool</span>
          <span className="font-mono">v0.1.0</span>
        </div>
      </footer>
    </div>
  );
}
