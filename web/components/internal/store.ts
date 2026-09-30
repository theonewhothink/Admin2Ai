"use client";

/**
 * The internal dashboard's data, shared by the sidebar (badges) and the pages,
 * loaded once and reloaded by the Refresh button. Reads go through
 * web/lib/api.ts, so they come from the in-browser engine on the static site
 * and from the backend otherwise.
 */
import { useEffect, useSyncExternalStore } from "react";
import { getInternalAcceptance, getInternalOperations, getInternalOverview } from "@/lib/api";
import type { InternalAcceptance, InternalOperations, InternalOverview } from "@/lib/internal-types";

export interface ResourceState<T> {
  data: T | null;
  loading: boolean;
  /** True once the first load finished (data may still be null: nothing to ask). */
  loaded: boolean;
}

const INITIAL = { data: null, loading: false, loaded: false } as const;

function createResource<T>(load: () => Promise<T | null>) {
  let state: ResourceState<T> = INITIAL;
  let inflight: Promise<void> | null = null;
  const listeners = new Set<() => void>();

  const set = (next: ResourceState<T>) => {
    state = next;
    listeners.forEach((l) => l());
  };
  const subscribe = (listener: () => void) => {
    listeners.add(listener);
    return () => {
      listeners.delete(listener);
    };
  };
  const snapshot = () => state;
  const serverSnapshot = () => INITIAL as ResourceState<T>;

  function refresh(): Promise<void> {
    if (inflight) return inflight;
    set({ ...state, loading: true });
    inflight = load()
      .then(
        (data) => set({ data, loading: false, loaded: true }),
        () => set({ ...state, loading: false, loaded: true }),
      )
      .finally(() => {
        inflight = null;
      });
    return inflight;
  }

  function useResource(): ResourceState<T> & { refresh: () => Promise<void> } {
    const current = useSyncExternalStore(subscribe, snapshot, serverSnapshot);
    useEffect(() => {
      if (!state.loaded && !inflight) void refresh();
    }, []);
    return { ...current, refresh };
  }

  /** Resolves when any load in flight has finished. */
  const settle = () => inflight ?? Promise.resolve();

  return { useResource, refresh, settle };
}

const overview = createResource<InternalOverview>(getInternalOverview);
const acceptance = createResource<InternalAcceptance>(getInternalAcceptance);
export const useAcceptance = acceptance.useResource;
export const useOverview = overview.useResource;
export const refreshOverview = overview.refresh;

let auditLimit: number | undefined;
const operations = createResource<InternalOperations>(() => getInternalOperations(auditLimit));
export const useOperations = operations.useResource;

/** Reload the operations page, optionally with more audit records. */
export async function refreshOperations(limit?: number): Promise<void> {
  const changed = limit !== undefined && limit !== auditLimit;
  if (limit !== undefined) auditLimit = limit;
  if (changed) await operations.settle(); // a load already running used the old limit
  return operations.refresh();
}
