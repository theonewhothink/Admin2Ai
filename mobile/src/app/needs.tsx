/**
 * Needs You items shared by Home (count), the tab badge and the Needs You
 * screen, so an answer updates all three at once.
 */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import type { Loaded } from "../api/client";
import type { NeedsYouItem } from "../api/types";
import { openItems } from "../models/needs";
import { useLoaded } from "./hooks";
import { useServices } from "./servicesContext";

interface NeedsState {
  loaded: Loaded<NeedsYouItem[]> | null;
  items: NeedsYouItem[];
  refreshing: boolean;
  refresh: () => Promise<void>;
  /** Hide an item the server accepted an answer for. */
  resolve: (id: string) => void;
}

const NeedsContext = createContext<NeedsState | null>(null);

export function NeedsProvider({ children }: { children: ReactNode }) {
  const { api } = useServices();
  const { loaded, refreshing, refresh } = useLoaded(() => api.getNeedsYou());
  // Answered ids stay hidden for the session, even if a refresh races the server.
  const [answered, setAnswered] = useState<ReadonlySet<string>>(new Set());

  const resolve = useCallback((id: string) => {
    setAnswered((prev) => new Set(prev).add(id));
  }, []);

  const value = useMemo<NeedsState>(
    () => ({ loaded, items: openItems(loaded?.data ?? [], answered), refreshing, refresh, resolve }),
    [loaded, answered, refreshing, refresh, resolve],
  );
  return <NeedsContext.Provider value={value}>{children}</NeedsContext.Provider>;
}

export function useNeeds(): NeedsState {
  const state = useContext(NeedsContext);
  if (!state) throw new Error("useNeeds must be used inside NeedsProvider");
  return state;
}
