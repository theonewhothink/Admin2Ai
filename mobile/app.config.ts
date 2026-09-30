/**
 * Store identity and the EAS project come from the environment; everything
 * else is in app.json (Expo passes it in as `config`).
 *
 *   APP_BUNDLE_ID        iOS bundle identifier   (default: app.json's placeholder eu.admin2ai.backoffice)
 *   APP_ANDROID_PACKAGE  Android package         (default: APP_BUNDLE_ID, then the placeholder)
 *   EAS_OWNER            Expo account or team that owns the project
 *   EAS_PROJECT_ID       EAS project id: required for Expo push tokens (notifications) and EAS Build
 *
 * The API and web addresses are EXPO_PUBLIC_* variables read by the app itself
 * (src/config.ts), set per EAS environment (see README "Release").
 */
import type { ConfigContext, ExpoConfig } from "expo/config";

const PLACEHOLDER_ID = "eu.admin2ai.backoffice";

function env(name: string): string | undefined {
  const value = process.env[name]?.trim();
  return value ? value : undefined;
}

export default ({ config }: ConfigContext): ExpoConfig => {
  const bundleIdentifier = env("APP_BUNDLE_ID") ?? config.ios?.bundleIdentifier ?? PLACEHOLDER_ID;
  const androidPackage = env("APP_ANDROID_PACKAGE") ?? env("APP_BUNDLE_ID") ?? config.android?.package ?? PLACEHOLDER_ID;
  const owner = env("EAS_OWNER") ?? config.owner;
  const projectId = env("EAS_PROJECT_ID");
  const extra = (config.extra ?? {}) as { eas?: Record<string, unknown> } & Record<string, unknown>;
  return {
    ...config,
    name: config.name ?? "Back Office",
    slug: config.slug ?? "backoffice",
    ...(owner ? { owner } : {}),
    ios: { ...config.ios, bundleIdentifier },
    android: { ...config.android, package: androidPackage },
    extra: { ...extra, eas: { ...(extra.eas ?? {}), ...(projectId ? { projectId } : {}) } },
  };
};
