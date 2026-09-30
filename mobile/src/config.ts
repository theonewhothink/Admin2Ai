/**
 * Runtime configuration. EXPO_PUBLIC_* variables are inlined by Expo at build
 * time. Without EXPO_PUBLIC_API_URL the app runs in demo mode on sample data,
 * asks nobody to sign in, and nothing leaves the phone.
 */
import { emitUnauthorized } from "./auth/events";
import { normalizeBaseUrl, type ApiEndpoint } from "./api/http";

export const API_BASE_URL: string | null = normalizeBaseUrl(process.env.EXPO_PUBLIC_API_URL);

/** The web app (sign-up, reconnecting email or bank). Defaults to the API's origin. */
export const WEB_URL: string | null = normalizeBaseUrl(process.env.EXPO_PUBLIC_WEB_URL) ?? API_BASE_URL;

/** Where owners write when they need a person (e.g. a forgotten password). */
export const SUPPORT_EMAIL: string = (process.env.EXPO_PUBLIC_SUPPORT_EMAIL ?? "").trim();

/** Ordinary API calls. Uploads use their own, longer timeout. */
export const API_TIMEOUT_MS = 6_000;

/** A signed-in owner is required whenever a server is configured. */
export const REQUIRES_SIGN_IN = API_BASE_URL !== null;

export function apiEndpoint(getAuthToken?: () => Promise<string | null>): ApiEndpoint {
  const endpoint: ApiEndpoint = { baseUrl: API_BASE_URL, timeoutMs: API_TIMEOUT_MS, onUnauthorized: emitUnauthorized };
  return getAuthToken ? { ...endpoint, getAuthToken } : endpoint;
}
