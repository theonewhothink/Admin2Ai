/**
 * Who may open the internal dashboard ("Admin OS", /internal/). This file is
 * the only place that decides it; the layout, the profile menu link and
 * anything else that shows the dashboard ask `canOpenInternal`.
 *
 * - Static demo site (NEXT_PUBLIC_ENGINE=browser): open. The whole engine runs
 *   in the visitor's own browser on the fixed demo business, so nothing
 *   private is behind it.
 * - With a backend: admins only. The role comes from the signed-in session
 *   (`viewerRole`, GET /api/auth/me). Without a session the dashboard stays
 *   closed. The server enforces the same rule on every /api/internal/ path
 *   (admin role required in production mode).
 */
import { browserEngine } from "./mode";
import type { Session } from "./owner";

export type Role = "owner" | "accountant" | "admin";

export function canOpenInternal(role: Role | null): boolean {
  if (browserEngine) return true;
  return role === "admin";
}

/** The signed-in person's role, or null when nobody is signed in. */
export function viewerRole(session: Session | null): Role | null {
  const role = session?.role;
  return role === "owner" || role === "accountant" || role === "admin" ? role : null;
}
