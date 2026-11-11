import { boundaryTransition, type BoundaryState } from "../state/errorBoundary";
import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";

interface Props {
  children: ReactNode;
}

class ErrorBoundary extends Component<Props, BoundaryState> {
  constructor(props: Props) {
    super(props);
    this.state = { status: "ready" };
  }

  static getDerivedStateFromError(): BoundaryState {
    return boundaryTransition({ status: "ready" }, "crash");
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error("ErrorBoundary caught an error:", error, errorInfo);
  }

  render() {
    if (this.state.status === "crashed") {
      return (
        <div className="flex flex-col items-center justify-center min-h-screen bg-surface text-ink">
          <h1 className="text-2xl font-semibold mb-2">Something went wrong</h1>
          <p className="text-ink-muted mb-6">
            An unexpected error occurred. Please reload the page to try again.
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="px-4 py-2 rounded-md bg-accent text-white font-medium hover:bg-accent-bright transition-colors cursor-pointer"
          >
            Reload page
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}

export default ErrorBoundary;
