/** Small data hooks shared by the screens. */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Loaded } from "../api/client";
import type { QueueSummary } from "../offline/pipeline";
import { useServices } from "./servicesContext";

export interface LoadState<T> {
  loaded: Loaded<T> | null;
  refreshing: boolean;
  refresh: () => Promise<void>;
}

/** Load once on mount; `refresh` reloads (pull to refresh). */
export function useLoaded<T>(load: () => Promise<Loaded<T>>): LoadState<T> {
  const [loaded, setLoaded] = useState<Loaded<T> | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const loadRef = useRef(load);
  loadRef.current = load;
  const mounted = useRef(true);

  const refresh = useCallback(async () => {
    setRefreshing(true);
    try {
      const next = await loadRef.current();
      if (mounted.current) setLoaded(next);
    } finally {
      if (mounted.current) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
    };
  }, [refresh]);

  return { loaded, refreshing, refresh };
}

/** Live view of the offline queue for the Scan screen. */
export function useQueueSummary(): QueueSummary | null {
  const { offline } = useServices();
  const [summary, setSummary] = useState<QueueSummary | null>(null);
  useEffect(() => {
    let active = true;
    const stop = offline.pipeline.subscribe((s) => {
      if (active) setSummary(s);
    });
    void offline.pipeline
      .summary()
      .then((s) => {
        if (active) setSummary(s);
      })
      .catch(() => undefined);
    return () => {
      active = false;
      stop();
    };
  }, [offline]);
  return summary;
}
