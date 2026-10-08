import type { NextConfig } from "next";
import { modeFrom } from "./lib/mode";

/**
 * Two ways to build:
 *
 * - Default: a Next.js server app (API over HTTP, or sample data). With
 *   NEXT_PUBLIC_API_URL and NEXT_PUBLIC_REQUIRE_SIGNIN=1 it is the production
 *   app: sign-in, onboarding, account (see lib/mode.ts).
 * - NEXT_PUBLIC_ENGINE=browser: a static export for GitHub Pages. The real
 *   Python engine runs in the browser (see lib/engine.ts), so no server is
 *   needed. NEXT_PUBLIC_BASE_PATH (e.g. /Admin2Ai) serves it from a sub-path.
 */
const mode = modeFrom({
  engine: process.env.NEXT_PUBLIC_ENGINE,
  apiUrl: process.env.NEXT_PUBLIC_API_URL,
  requireSignin: process.env.NEXT_PUBLIC_REQUIRE_SIGNIN,
});
const staticSite = mode === "demo";
const basePath = (process.env.NEXT_PUBLIC_BASE_PATH ?? "").replace(/\/+$/, "");

/** The API's origin, for the Content-Security-Policy. */
function apiOrigin(): string {
  try {
    return process.env.NEXT_PUBLIC_API_URL ? new URL(process.env.NEXT_PUBLIC_API_URL).origin : "";
  } catch {
    return "";
  }
}

/**
 * Security headers for server builds (a static export cannot set headers;
 * GitHub Pages serves the demo). Next.js needs inline scripts for hydration
 * and inline styles for style props, hence 'unsafe-inline' without nonces;
 * everything else is locked to this origin, the API, and Anthropic (the chat
 * can call Claude from the browser with the owner's own key).
 */
function securityHeaders(): { key: string; value: string }[] {
  const dev = process.env.NODE_ENV !== "production";
  const api = apiOrigin();
  const csp = [
    "default-src 'self'",
    `script-src 'self' 'unsafe-inline'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src ${["'self'", api, "https://api.anthropic.com", dev ? "ws: wss:" : ""].filter(Boolean).join(" ")}`,
    "media-src 'self' blob:",
    "worker-src 'self' blob:",
    "manifest-src 'self'",
    "frame-src 'none'",
    "object-src 'none'",
    "base-uri 'self'",
    `form-action ${["'self'", api].filter(Boolean).join(" ")}`,
    "frame-ancestors 'none'",
  ].join("; ");
  return [
    { key: "Content-Security-Policy", value: csp },
    { key: "Strict-Transport-Security", value: "max-age=63072000; includeSubDomains" },
    { key: "X-Frame-Options", value: "DENY" },
    { key: "X-Content-Type-Options", value: "nosniff" },
    { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
    {
      key: "Permissions-Policy",
      value: "camera=(), microphone=(), geolocation=(), payment=(), usb=(), serial=(), bluetooth=(), browsing-topics=()",
    },
    { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
  ];
}

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  // `page.prod.tsx` files (sign-in, sign-up) exist only in server builds: the
  // static demo keeps exactly its routes and has no sign-in screens.
  pageExtensions: staticSite ? ["tsx", "ts", "jsx", "js"] : ["prod.tsx", "tsx", "ts", "jsx", "js"],
  env: {
    // Lets browsers fetch the new engine bundle after each deploy.
    NEXT_PUBLIC_ENGINE_BUILD: process.env.NEXT_PUBLIC_ENGINE_BUILD ?? String(Date.now()),
  },
  ...(staticSite ? { output: "export" as const, trailingSlash: true, images: { unoptimized: true } } : {}),
  ...(basePath ? { basePath, assetPrefix: basePath } : {}),
  ...(staticSite ? {} : { headers: async () => [{ source: "/:path*", headers: securityHeaders() }] }),
};

export default nextConfig;
