/**
 * How this build runs. Decided once, at build time (Next.js inlines
 * NEXT_PUBLIC_* variables), and read from here everywhere else.
 *
 * - "demo":       NEXT_PUBLIC_ENGINE=browser. The static GitHub Pages site: the
 *                 real engine runs in the browser. No sign-in, no redirects.
 * - "production": NEXT_PUBLIC_API_URL set and NEXT_PUBLIC_REQUIRE_SIGNIN=1.
 *                 Every page needs a signed-in user; data is loaded in the
 *                 browser with the session cookie, never from sample data.
 * - "api":        NEXT_PUBLIC_API_URL set without sign-in. A local backend in
 *                 demo mode; server-rendered, falls back to sample data.
 * - "sample":     nothing set. Sample data only.
 *
 * Safe to import from Server and Client Components, and from next.config.ts.
 */

export type AppMode = "demo" | "production" | "api" | "sample";

/** The backend's base URL without a trailing slash, or "" when there is none. */
export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").trim().replace(/\/+$/, "");

/** Sub-path the site is served from (e.g. "/Admin2Ai" on GitHub Pages), or "". */
export const BASE_PATH = (process.env.NEXT_PUBLIC_BASE_PATH ?? "").replace(/\/+$/, "");

export function modeFrom(env: {
  engine?: string;
  apiUrl?: string;
  requireSignin?: string;
}): AppMode {
  if (env.engine === "browser") return "demo";
  const api = (env.apiUrl ?? "").trim().replace(/\/+$/, "");
  if (!api) return "sample";
  return env.requireSignin === "1" ? "production" : "api";
}

export const mode: AppMode = modeFrom({
  engine: process.env.NEXT_PUBLIC_ENGINE,
  apiUrl: process.env.NEXT_PUBLIC_API_URL,
  requireSignin: process.env.NEXT_PUBLIC_REQUIRE_SIGNIN,
});

/** The static demo: the Python engine runs in the browser. */
export const browserEngine = mode === "demo";

/** Real customers, real sign-in. */
export const production = mode === "production";

/**
 * Pages load their data in the browser (the Live* components) instead of on
 * the server: on the static demo because there is no server, and in
 * production so every call carries the owner's session cookie straight to
 * the API, wherever the API is hosted.
 */
export const clientRendered = browserEngine || production;

/** Where owners write when they need a person (e.g. a forgotten password). */
export const SUPPORT_EMAIL = (process.env.NEXT_PUBLIC_SUPPORT_EMAIL ?? "").trim();
