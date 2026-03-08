import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import Toast from "./components/Toast";
import { ToastProvider, showToastGlobal } from "./hooks/useToast";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
    mutations: {
      onError: (error: Error) => {
        showToastGlobal(error.message || "An unexpected error occurred");
      },
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ToastProvider>
      <QueryClientProvider client={queryClient}>
        <App />
        <Toast />
      </QueryClientProvider>
    </ToastProvider>
  </StrictMode>,
);
