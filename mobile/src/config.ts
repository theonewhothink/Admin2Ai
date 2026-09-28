/**
 * Runtime configuration. `EXPO_PUBLIC_API_URL` is inlined by Expo at build time.
 * Without it the app runs in demo mode on sample data and nothing leaves the phone.
 */
import { normalizeBaseUrl, type ApiEndpoint } from "./api/http";

export const API_BASE_URL: string | null = normalizeBaseUrl(process.env.EXPO_PUBLIC_API_URL);

/** Ordinary API calls. Uploads use their own, longer timeout. */
export const API_TIMEOUT_MS = 6_000;

export function apiEndpoint(getAuthToken?: () => Promise<string | null>): ApiEndpoint {
  return getAuthToken
    ? { baseUrl: API_BASE_URL, timeoutMs: API_TIMEOUT_MS, getAuthToken }
    : { baseUrl: API_BASE_URL, timeoutMs: API_TIMEOUT_MS };
}
