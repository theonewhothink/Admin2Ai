"use client";

/**
 * The signed-in owner (production only). Loaded once for the app shell from
 * GET /api/auth/me; without a session the owner is sent to sign in. In the
 * demo and the sample site there is no provider and useSession() is null.
 */
import type { ReactNode } from "react";
import { useData } from "@/components/live/useData";
import { requireSession } from "@/lib/account";
import { SessionContext } from "./context";

export { useSession } from "./context";

// A server hiccup must not take the header down with it: the page says what failed.
const loadSession = () => requireSession().catch(() => null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const session = useData(loadSession);
  return <SessionContext.Provider value={session}>{children}</SessionContext.Provider>;
}
