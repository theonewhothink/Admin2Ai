"use client";

import { useEffect, useState } from "react";

/**
 * Load data in the browser. Returns null until `load(...args)` resolves, and
 * again whenever the arguments change. `load` should be a module-level
 * function so it stays the same between renders.
 */
export function useData<A extends unknown[], T>(load: (...args: A) => Promise<T>, ...args: A): T | null {
  const key = JSON.stringify(args);
  const [state, setState] = useState<{ key: string; data: T } | null>(null);
  useEffect(() => {
    let live = true;
    void load(...(JSON.parse(key) as A)).then((data) => {
      if (live) setState({ key, data });
    });
    return () => {
      live = false;
    };
  }, [load, key]);
  return state !== null && state.key === key ? state.data : null;
}
