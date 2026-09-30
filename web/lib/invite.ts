/**
 * An accountant's invitation link (§29): `/invite#<token>`. The token sits in the fragment, so it never
 * reaches a server log or a Referer header; the page keeps it in sessionStorage while the owner signs up
 * or signs in, then accepts it once (POST /api/invitations/accept).
 */

const KEY = "admin2ai:invitation";
const TOKEN = /^[A-Za-z0-9_-]{32,128}$/;

/** The token in the address bar (then removed from it), else the one kept for this visit. */
export function takeInvitationToken(): string | null {
  if (typeof window === "undefined") return null;
  const fromHash = window.location.hash.replace(/^#/, "");
  if (TOKEN.test(fromHash)) {
    try {
      window.sessionStorage.setItem(KEY, fromHash);
    } catch {
      // Storage unavailable: the token still works on this page.
    }
    window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
    return fromHash;
  }
  return pendingInvitation();
}

/** An invitation waiting to be accepted in this visit (after sign-up or sign-in). */
export function pendingInvitation(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const kept = window.sessionStorage.getItem(KEY);
    return kept && TOKEN.test(kept) ? kept : null;
  } catch {
    return null;
  }
}

export function forgetInvitation(): void {
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}
