"use client";

import { useCallback, useEffect, useState } from "react";
import { call } from "@/lib/api";

const FAILED = "This didn’t load. Try again in a moment.";

type State<T> = { path: string; version: number; status: number; data: T | null; error: string | null };

export interface ApiState<T> {
  /** The latest answer (kept while a reload is on its way, so nothing flashes). */
  data: T | null;
  /** The API's own plain message when the read failed. */
  error: string | null;
  /** HTTP status of the latest answer (0 until the first one). */
  status: number;
  /** True until the first answer for this path arrives. */
  loading: boolean;
  /** Read it again (after a change). */
  reload: () => void;
}

/**
 * One read from the engine (demo) or the API (production) for a detail page or a settings card.
 * In production a missing session goes to sign-in (lib/api.ts `call`); every other failure comes
 * back as the API's own plain message, never as sample data.
 */
export function useApi<T>(path: string | null): ApiState<T> {
  const [version, setVersion] = useState(0);
  const [state, setState] = useState<State<T> | null>(null);
  useEffect(() => {
    if (!path) return;
    let live = true;
    call<T>("GET", path).then((r) => {
      if (!live) return;
      const message = (r.body as { message?: unknown } | null)?.message;
      setState({
        path,
        version,
        status: r.status,
        data: r.ok ? r.body : null,
        error: r.ok ? null : typeof message === "string" && message ? message : FAILED,
      });
    });
    return () => {
      live = false;
    };
  }, [path, version]);
  const reload = useCallback(() => setVersion((v) => v + 1), []);
  const current = state && state.path === path ? state : null;
  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    status: current?.status ?? 0,
    loading: current === null,
    reload,
  };
}

/** A change sent to the engine or the API: `{ ok, message }`, the message always plain words. */
export async function send<T = Record<string, unknown>>(
  path: string,
  body: unknown,
): Promise<{ ok: boolean; message: string; body: T & { message?: string } }> {
  const r = await call<T & { message?: string }>("POST", path, body);
  const message = typeof r.body?.message === "string" ? r.body.message : r.ok ? "Done." : "I couldn’t save that. Try again.";
  return { ok: r.ok, message, body: r.body };
}
