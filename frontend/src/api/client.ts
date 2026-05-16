function normalizeBasePath(basePath: string): string {
  if (!basePath || basePath === "/") {
    return "";
  }
  return basePath.endsWith("/") ? basePath.slice(0, -1) : basePath;
}

function joinPath(basePath: string, path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${basePath}${normalizedPath}`;
}

const APP_BASE_PATH = normalizeBasePath(import.meta.env.BASE_URL);

export const ROUTER_BASENAME = APP_BASE_PATH || undefined;
const API_BASE_URL = joinPath(APP_BASE_PATH, "/api");
const HEALTH_URL = joinPath(APP_BASE_PATH, "/health");
const CSRF_COOKIE_NAME = "anon_session_csrf";
const CSRF_HEADER_NAME = "X-CSRF-Token";

export function apiUrl(path: string): string {
  return joinPath(API_BASE_URL, path);
}

function readCookie(name: string): string | null {
  const prefix = `${encodeURIComponent(name)}=`;
  const cookie = document.cookie
    .split(";")
    .map((value) => value.trim())
    .find((value) => value.startsWith(prefix));
  return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : null;
}

function isUnsafeMethod(method: string | undefined): boolean {
  return !["GET", "HEAD", "OPTIONS"].includes((method ?? "GET").toUpperCase());
}

async function refreshCsrfToken(): Promise<string | null> {
  try {
    await fetch(HEALTH_URL, {
      cache: "no-store",
      credentials: "same-origin",
    });
  } catch {
    return readCookie(CSRF_COOKIE_NAME);
  }
  return readCookie(CSRF_COOKIE_NAME);
}

async function csrfTokenForRequest(
  method: string | undefined,
): Promise<string | null> {
  if (!isUnsafeMethod(method)) {
    return null;
  }
  return readCookie(CSRF_COOKIE_NAME) ?? refreshCsrfToken();
}

function requestHeaders(
  options: RequestInit | undefined,
  csrfToken: string | null,
): HeadersInit {
  return {
    ...(options?.body instanceof FormData
      ? {}
      : { "Content-Type": "application/json" }),
    ...(csrfToken ? { [CSRF_HEADER_NAME]: csrfToken } : {}),
    ...options?.headers,
  };
}

async function sendApiRequest(
  url: string,
  options: RequestInit | undefined,
  csrfToken: string | null,
): Promise<Response> {
  return fetch(url, {
    ...options,
    credentials: options?.credentials ?? "same-origin",
    headers: requestHeaders(options, csrfToken),
  });
}

async function isCsrfFailure(response: Response): Promise<boolean> {
  if (response.status !== 403) {
    return false;
  }
  try {
    const errorBody = await response.clone().json();
    return errorBody.detail === "CSRF validation failed";
  } catch {
    return false;
  }
}

class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export async function apiFetch<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const url = apiUrl(path);
  const unsafeRequest = isUnsafeMethod(options?.method);
  let csrfToken = await csrfTokenForRequest(options?.method);
  let response = await sendApiRequest(url, options, csrfToken);

  if (unsafeRequest && (await isCsrfFailure(response))) {
    csrfToken = readCookie(CSRF_COOKIE_NAME) ?? (await refreshCsrfToken());
    if (csrfToken) {
      response = await sendApiRequest(url, options, csrfToken);
    }
  }

  if (!response.ok) {
    let detail = `Request failed with status ${response.status}`;
    try {
      const errorBody = await response.json();
      detail = errorBody.detail || detail;
    } catch {
      // ignore parse errors
    }
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json();
}
