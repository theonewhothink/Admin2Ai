/**
 * Which screen a tapped notification opens. The server sends notifications
 * only for (§42): a new hard approval (changed bank details, a payment to
 * approve), a connection that needs reconnecting, a month closed.
 *
 * Accepted payload `data` (any one is enough, first match wins):
 *   { screen: "needs-you" | "connections" | "home" | "activity" }
 *   { url: "/needs-you" | "/connections" | "/" | "/activity" }   (also "backoffice://needs-you")
 *   { type | kind: "approval" | "hard_approval" | "payment_approval" | "bank_details_changed" | "needs_you"
 *                | "connection" | "reconnect" | "connection_stale" | "connection_needs_reconnect"
 *                | "month_closed" }
 * Anything else opens nothing in particular (the app just comes forward).
 */

export type NotificationTarget = "/" | "/needs-you" | "/connections" | "/activity";

const SCREENS: Record<string, NotificationTarget> = {
  home: "/",
  "": "/",
  "needs-you": "/needs-you",
  needs_you: "/needs-you",
  connections: "/connections",
  activity: "/activity",
};

const KINDS: Record<string, NotificationTarget> = {
  approval: "/needs-you",
  hard_approval: "/needs-you",
  payment_approval: "/needs-you",
  bank_details_changed: "/needs-you",
  needs_you: "/needs-you",
  connection: "/connections",
  reconnect: "/connections",
  connection_stale: "/connections",
  connection_needs_reconnect: "/connections",
  month_closed: "/",
};

function fromUrl(url: string): NotificationTarget | null {
  let rest = url.trim();
  const scheme = /^([a-z][a-z0-9+.-]*):\/\//i.exec(rest);
  if (scheme) {
    rest = rest.slice(scheme[0].length);
    // http(s): drop the host. An app link (backoffice://needs-you) names the screen where the host would be.
    if (/^https?$/i.test(scheme[1] ?? "")) rest = rest.replace(/^[^/]*/, "");
  }
  const path = rest.replace(/[?#].*$/, "").replace(/^\/+|\/+$/g, "").toLowerCase();
  return SCREENS[path] ?? null;
}

export function routeForNotification(data: unknown): NotificationTarget | null {
  if (typeof data !== "object" || data === null) return null;
  const d = data as Record<string, unknown>;
  if (typeof d.screen === "string") {
    const hit = SCREENS[d.screen.trim().toLowerCase()];
    if (hit) return hit;
  }
  if (typeof d.url === "string") {
    const hit = fromUrl(d.url);
    if (hit) return hit;
  }
  for (const key of ["type", "kind"] as const) {
    const v = d[key];
    if (typeof v === "string") {
      const hit = KINDS[v.trim().toLowerCase().replace(/[-\s]/g, "_")];
      if (hit) return hit;
    }
  }
  return null;
}
