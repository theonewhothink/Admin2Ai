"use client";

import { createContext, useContext } from "react";
import type { Session } from "@/lib/owner";

/** Filled by SessionProvider in production; null everywhere else. */
export const SessionContext = createContext<Session | null>(null);

export function useSession(): Session | null {
  return useContext(SessionContext);
}
