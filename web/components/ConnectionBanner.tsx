"use client";

import { useEffect, useState } from "react";
import { call } from "@/lib/api";
import { formatDayShort, formatTime } from "@/lib/format";
import { production } from "@/lib/mode";
import type { Connection } from "@/lib/types";
import { Icon } from "./Icon";
import styles from "./ConnectionBanner.module.css";

type Phase = "stale" | "connecting" | "done" | "gone";

const what: Record<Connection["kind"], string> = {
  email: "Your email",
  bank: "Your bank",
  accountant: "Your accountant’s inbox",
};

/**
 * Production: ask the API to reconnect. It either answers with a consent page
 * to open (authorizeUrl / redirectUrl) or says the connection works again.
 * Never shows "connected again" unless the API said so.
 */
async function reconnectForReal(id: string): Promise<{ ok: boolean; go?: string; message?: string }> {
  const r = await call<{ authorizeUrl?: string; redirectUrl?: string; message?: string }>(
    "POST",
    `/api/connections/${encodeURIComponent(id)}/reconnect`,
    {},
  );
  const go = r.body.authorizeUrl ?? r.body.redirectUrl;
  if (r.ok && typeof go === "string" && /^https?:\/\//.test(go)) return { ok: true, go };
  return { ok: r.ok, message: r.ok ? undefined : (r.body.message ?? "I couldn’t reconnect it. Try again in a moment.") };
}

export function ConnectionBanner({ connection }: { connection: Connection }) {
  const [phase, setPhase] = useState<Phase>("stale");
  const [problem, setProblem] = useState<string | null>(null);

  const reconnect = async () => {
    setPhase("connecting");
    setProblem(null);
    if (!production) return; // The demo reconnects on a timer (below).
    const r = await reconnectForReal(connection.id);
    if (r.go) {
      window.location.assign(r.go);
      return;
    }
    if (r.ok) setPhase("done");
    else {
      setPhase("stale");
      setProblem(r.message ?? null);
    }
  };

  useEffect(() => {
    if (production) {
      if (phase === "done") {
        const t = setTimeout(() => setPhase("gone"), 2600);
        return () => clearTimeout(t);
      }
      return;
    }
    if (phase === "connecting") {
      const t = setTimeout(() => setPhase("done"), 1100);
      return () => clearTimeout(t);
    }
    if (phase === "done") {
      const t = setTimeout(() => setPhase("gone"), 2600);
      return () => clearTimeout(t);
    }
  }, [phase]);

  const since =
    connection.lastSyncedLabel ??
    `${formatTime(connection.lastSyncedAt)} on ${formatDayShort(connection.lastSyncedAt.slice(0, 10))}`;

  return (
    <div className={styles.wrap} data-phase={phase} aria-live="polite">
      <div className={styles.inner}>
        <div className={`notice ${phase === "done" ? "notice-good" : "notice-attention"} ${styles.banner}`}>
          <span className={styles.icon} data-tone={phase === "done" ? "good" : "attention"}>
            <Icon name={phase === "done" ? "check" : connection.kind === "bank" ? "bank" : "mail"} size={18} strokeWidth={1.8} />
          </span>
          {phase === "done" || phase === "gone" ? (
            <p className={styles.text}>
              <strong>{connection.name} is connected again.</strong> Catching up now.
            </p>
          ) : (
            <p className={styles.text}>
              <strong>{connection.name} needs reconnecting.</strong> {what[connection.kind]} has not synced since {since}.
              {problem ? <> {problem}</> : null}
            </p>
          )}
          {phase === "stale" || phase === "connecting" ? (
            <button
              type="button"
              className={`btn btn-secondary ${styles.action}`}
              onClick={() => void reconnect()}
              disabled={phase === "connecting"}
            >
              {phase === "connecting" ? "Reconnecting…" : "Reconnect"}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
