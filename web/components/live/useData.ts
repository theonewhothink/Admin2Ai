"use client";

import { useEffect, useState } from "react";

type State<T> = { key: string; data: T } | { key: string; error: unknown };

/**
 * Load data in the browser. Returns null until `load(...args)` resolves, and
 * again whenever the arguments change. `load` should be a module-level
 * function so it stays the same between renders.
 *
 * If `load` rejects (production: the API could not answer), the error is
 * thrown during render so the nearest error boundary shows its plain
 * "This page didn't load." screen instead of a spinner that never ends.
 * The demo's loaders never reject: they fall back to sample data.
 */
export function useData<A extends unknown[], T>(load: (...args: A) => Promise<T>, ...args: A): T | null {
  const key = JSON.stringify(args);
  const [state, setState] = useState<State<T> | null>(null);
  useEffect(() => {
    let live = true;
    load(...(JSON.parse(key) as A)).then(
      (data) => {
        if (live) setState({ key, data });
      },
      (error: unknown) => {
        if (live) setState({ key, error });
      },
    );
    return () => {
      live = false;
    };
  }, [load, key]);
  if (state === null || state.key !== key) return null;
  if ("error" in state) throw state.error;
  return state.data;
}
