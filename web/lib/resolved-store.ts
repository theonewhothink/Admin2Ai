"use client";

/**
 * Remembers which needs-you items were answered in this browser session, so
 * counts on Home, the nav badge and company statuses stay consistent while the
 * app runs on sample data. With a live backend the answered items simply stop
 * coming back from the API, and this store changes nothing.
 */
import { useSyncExternalStore } from "react";

const KEY = "admin2ai:answered";
const EMPTY: ReadonlySet<string> = new Set();
const listeners = new Set<() => void>();
let current: ReadonlySet<string> | null = null;

function read(): ReadonlySet<string> {
  if (current) return current;
  try {
    const raw = window.sessionStorage.getItem(KEY);
    const ids: unknown = raw ? JSON.parse(raw) : [];
    current = new Set(Array.isArray(ids) ? ids.filter((x): x is string => typeof x === "string") : []);
  } catch {
    current = new Set();
  }
  return current;
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function markAnswered(id: string) {
  const next = new Set(read());
  next.add(id);
  current = next;
  try {
    window.sessionStorage.setItem(KEY, JSON.stringify([...next]));
  } catch {
    // Storage can be unavailable (private mode); the in-memory set still works.
  }
  listeners.forEach((l) => l());
}

export function resetAnswered() {
  current = new Set();
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    // ignore
  }
  listeners.forEach((l) => l());
}

export function useAnswered(): ReadonlySet<string> {
  return useSyncExternalStore(subscribe, read, () => EMPTY);
}

/** Number of ids still open. */
export function useOpenCount(ids: readonly string[]): number {
  const answered = useAnswered();
  return ids.filter((id) => !answered.has(id)).length;
}
