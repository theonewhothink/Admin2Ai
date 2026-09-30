/**
 * Minimal HTTP port shared by the API client and the evidence uploader.
 * Production wraps `expo/fetch` (which accepts Uint8Array bodies); tests pass fakes.
 */

export interface HttpRequest {
  method: "GET" | "POST";
  url: string;
  headers: Record<string, string>;
  body?: string | Uint8Array;
  timeoutMs: number;
}

export interface HttpResponse {
  status: number;
  header(name: string): string | null;
  text(): Promise<string>;
}

/** Rejects only for transport failures (offline, DNS, TLS, timeout). HTTP errors resolve. */
export type HttpSend = (request: HttpRequest) => Promise<HttpResponse>;

export interface ApiEndpoint {
  /** Base URL such as "https://api.example.eu", or null for demo mode (sample data only). */
  baseUrl: string | null;
  timeoutMs: number;
  /** Bearer token for the signed-in owner, if any. */
  getAuthToken?: () => Promise<string | null>;
  /** Called when the server answers 401: the session is missing or has ended. */
  onUnauthorized?: () => void;
}

/** Normalise a configured base URL: trims, drops trailing slashes, requires http(s). */
export function normalizeBaseUrl(value: string | undefined | null): string | null {
  const v = (value ?? "").trim().replace(/\/+$/, "");
  if (!v) return null;
  return /^https?:\/\/[^\s/]+/i.test(v) ? v : null;
}

export async function authHeaders(endpoint: ApiEndpoint): Promise<Record<string, string>> {
  const token = endpoint.getAuthToken ? await endpoint.getAuthToken() : null;
  return token ? { Authorization: `Bearer ${token}` } : {};
}

export function parseJson(text: string): unknown {
  if (!text.trim()) return {};
  try {
    return JSON.parse(text) as unknown;
  } catch {
    return undefined;
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
