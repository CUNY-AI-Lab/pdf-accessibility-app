import { BrowserRouter, Link, Route, Routes } from "react-router-dom";
import { ROUTER_BASENAME } from "./api/client";
import ErrorBoundary from "./components/ErrorBoundary";
import Layout from "./components/Layout";
import DashboardPage from "./pages/DashboardPage";
import JobDetailPage from "./pages/JobDetailPage";
import ReviewPage from "./pages/ReviewPage";
import UploadPage from "./pages/UploadPage";

function NotFound() {
  return (
    <div className="text-center py-20 animate-fade-in">
      <h1 className="text-5xl text-ink mb-2">404</h1>
      <p className="text-ink-muted mb-6">Page not found</p>
      <Link
        to="/"
        className="text-sm text-accent hover:text-accent-bright font-medium no-underline"
      >
        Go home
      </Link>
    </div>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter basename={ROUTER_BASENAME}>
        <Routes>
          <Route element={<Layout />}>
            <Route path="/" element={<UploadPage />} />
            <Route path="/dashboard" element={<DashboardPage />} />
            <Route path="/jobs/:id" element={<JobDetailPage />} />
            <Route path="/jobs/:id/review" element={<ReviewPage />} />
            <Route path="*" element={<NotFound />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ErrorBoundary>
  );
}
