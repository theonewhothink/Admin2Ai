/** The signed-in user as the app shows them (production). No network code here. */
import type { Owner } from "./types";

export interface SessionUser {
  id: string;
  email: string;
  name: string;
}

export interface Session {
  user: SessionUser;
  tenant: { id: string; name: string };
  /** "owner", "accountant" or "admin". */
  role: string;
}

/** The header's avatar and name, from the signed-in user. */
export function ownerFrom(session: Session): Owner {
  const name = session.user.name.trim() || session.user.email.split("@")[0] || session.user.email;
  const parts = name.split(/\s+/).filter(Boolean);
  const first = parts[0] ?? name;
  const last = parts.length > 1 ? (parts[parts.length - 1] ?? "") : "";
  const initials = (Array.from(first)[0] ?? "") + (last ? (Array.from(last)[0] ?? "") : "");
  return { firstName: first, fullName: name, email: session.user.email, initials: initials.toUpperCase() || "?" };
}
