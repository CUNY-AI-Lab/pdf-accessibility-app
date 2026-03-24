import { Link, NavLink, Outlet } from "react-router-dom";

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  `px-3 py-1.5 rounded-lg text-sm font-medium transition-all duration-200 no-underline ${
    isActive
      ? "bg-accent-light text-accent"
      : "text-ink-muted hover:text-ink hover:bg-paper-warm"
  }`;

export default function Layout() {
  return (
    <div className="min-h-screen flex flex-col">
      {/* Beta banner */}
      <div className="bg-amber-100 text-amber-900 text-center text-sm py-1.5 px-4 font-medium">
        This tool is currently in beta. Results may vary — please review output carefully.
      </div>

      {/* Header */}
      <header className="border-b border-ink/8 bg-cream/80 backdrop-blur-sm sticky top-0 z-50">
        <div className="max-w-6xl mx-auto px-6 h-16 flex items-center justify-between">
          <Link to="/" className="flex items-center gap-3 no-underline group shrink-0">
            <img
              src={`${import.meta.env.BASE_URL}cuny-ai-lab-logo.png`}
              alt="CUNY AI Lab"
              className="h-7 w-auto transition-opacity duration-200 group-hover:opacity-80"
            />
            <div className="hidden sm:block w-px h-6 bg-ink/10" />
            <span className="hidden sm:inline font-display text-base font-semibold tracking-tight text-ink">
              PDF Accessibility
            </span>
          </Link>

          <nav className="flex items-center gap-1">
            <NavLink to="/" end className={navLinkClass}>
              Upload
            </NavLink>
            <NavLink to="/dashboard" className={navLinkClass}>
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
