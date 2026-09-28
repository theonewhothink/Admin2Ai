import type { NextConfig } from "next";

/**
 * Two ways to build:
 *
 * - Default: a Next.js server app (API over HTTP, or sample data).
 * - NEXT_PUBLIC_ENGINE=browser: a static export for GitHub Pages. The real
 *   Python engine runs in the browser (see lib/engine.ts), so no server is
 *   needed. NEXT_PUBLIC_BASE_PATH (e.g. /Admin2Ai) serves it from a sub-path.
 */
const staticSite = process.env.NEXT_PUBLIC_ENGINE === "browser";
const basePath = (process.env.NEXT_PUBLIC_BASE_PATH ?? "").replace(/\/+$/, "");

const nextConfig: NextConfig = {
  reactStrictMode: true,
  poweredByHeader: false,
  env: {
    // Lets browsers fetch the new engine bundle after each deploy.
    NEXT_PUBLIC_ENGINE_BUILD: process.env.NEXT_PUBLIC_ENGINE_BUILD ?? String(Date.now()),
  },
  ...(staticSite ? { output: "export" as const, trailingSlash: true, images: { unoptimized: true } } : {}),
  ...(basePath ? { basePath, assetPrefix: basePath } : {}),
};

export default nextConfig;
