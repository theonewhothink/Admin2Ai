/**
 * Who may open the internal dashboard ("Admin OS", /internal/). This file is
 * the only place that decides it; the layout, the profile menu link and
 * anything else that shows the dashboard ask `canOpenInternal`.
 *
 * - Static demo site (NEXT_PUBLIC_ENGINE=browser): open. The whole engine runs
 *   in the visitor's own browser on the fixed demo business, so nothing
 *   private is behind it.
 * - With a backend: admins only. The role comes from the signed-in session
 *   (`viewerRole`). Until sign-in exists there is no session, so the dashboard
 *   stays closed. The backend's counterpart is `admin_only()` in
 *   backend/src/backoffice/internal.py, which marks every /api/internal/ path.
 */
import { browserEngine } from "./engine";

export type Role = "owner" | "accountant" | "admin";

export function canOpenInternal(role: Role | null): boolean {
  if (browserEngine) return true;
  return role === "admin";
}

/** The signed-in person's role, or null when nobody is signed in (always, until sign-in exists). */
export function viewerRole(): Role | null {
  return null;
}
