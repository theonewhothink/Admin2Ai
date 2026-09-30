"use client";

import { useEffect, useState } from "react";
import { call } from "@/lib/api";
import { formatDayShort, formatTime } from "@/lib/format";
import { production } from "@/lib/mode";
import type { Connection } from "@/lib/types";
import { Icon } from "./Icon";
import styles from "./ConnectionBanner.module.css";

type Phase = "stale" | "connecting" | "catching_up" | "done" | "gone";

const what: Record<Connection["kind"], string> = {
  email: "Your email",
  bank: "Your bank",
  accountant: "Your accountant’s inbox",
};

/** Production: how often, and how long, to look for the first sync that works after reconnecting. */
const POLL_MS = 5000;
const POLL_TRIES = 24;

/**
 * Production: ask the API to reconnect. A Google or Microsoft mailbox answers with the provider's
 * sign-in page (authorizeUrl), where the owner signs in again. Other connections are tried again
 * with their saved sign-in. Neither means "connected": that is shown only once a sync has worked.
 */
async function reconnectForReal(id: string): Promise<{ ok: boolean; go?: string; message?: string }> {
  const r = await call<{ authorizeUrl?: string; redirectUrl?: string; message?: string }>(
    "POST",
    `/api/connections/${encodeURIComponent(id)}/reconnect`,
    {},
  );
  const go = r.body.authorizeUrl ?? r.body.redirectUrl;
  if (r.ok && typeof go === "string" && /^https?:\/\//.test(go)) return { ok: true, go };
  return { ok: r.ok, message: r.body.message ?? (r.ok ? undefined : "I couldn’t reconnect it. Try again in a moment.") };
}

export function ConnectionBanner({ connection }: { connection: Connection }) {
  const [phase, setPhase] = useState<Phase>(
    production && connection.reconnect === "catching_up" ? "catching_up" : "stale",
  );
  const [problem, setProblem] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const reconnect = async () => {
    setPhase("connecting");
    setProblem(null);
    if (!production) return; // The demo reconnects on a timer (below): its connections are simulated.
    const r = await reconnectForReal(connection.id);
    if (r.go) {
      window.location.assign(r.go); // the provider's sign-in; the owner comes back to Sources
      return;
    }
    if (r.ok) {
      setNote(r.message ?? null);
      setPhase("catching_up"); // tried again: "connected" only once a sync has worked (below)
    } else {
      setPhase("stale");
      setProblem(r.message ?? null);
    }
  };

  // Production: look for the first sync that works. Only the API saying "healthy" means connected again.
  useEffect(() => {
    if (!production || phase !== "catching_up") return;
    let stopped = false;
    let tries = 0;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = async () => {
      tries += 1;
      const r = await call<{ connections?: Connection[] }>("GET", "/api/connections");
      if (stopped) return;
      const current = r.ok ? r.body.connections?.find((c) => c.id === connection.id) : undefined;
      if (current?.status === "healthy") {
        setPhase("done");
        return;
      }
      if (current && current.reconnect !== "catching_up") {
        setPhase("stale"); // the sync was refused again: it needs the owner once more
        setProblem(current.message ?? null);
        return;
      }
      if (tries < POLL_TRIES) timer = setTimeout(() => void check(), POLL_MS);
    };
    timer = setTimeout(() => void check(), POLL_MS);
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [phase, connection.id]);

  useEffect(() => {
    if (!production && phase === "connecting") {
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
  const action = production ? (connection.action ?? "Reconnect") : "Reconnect";
  const signIn = production && action.startsWith("Sign in");

  let text: React.ReactNode;
  if (phase === "done" || phase === "gone") {
    text = production ? (
      <>
        <strong>{connection.name} is connected again.</strong> It synced.
      </>
    ) : (
      <>
        <strong>{connection.name} is connected again.</strong> Catching up now.
      </>
    );
  } else if (phase === "catching_up") {
    text = (
      <>
        <strong>{connection.name} is catching up.</strong>{" "}
        {note ?? "You signed in again. It shows as connected once it has synced."}
      </>
    );
  } else {
    text = (
      <>
        <strong>{connection.name} needs reconnecting.</strong> {what[connection.kind]} has not synced since {since}.
        {problem ? <> {problem}</> : null}
      </>
    );
  }

  return (
    <div className={styles.wrap} data-phase={phase} aria-live="polite">
      <div className={styles.inner}>
        <div className={`notice ${phase === "done" ? "notice-good" : "notice-attention"} ${styles.banner}`}>
          <span className={styles.icon} data-tone={phase === "done" ? "good" : "attention"}>
            <Icon name={phase === "done" ? "check" : connection.kind === "bank" ? "bank" : "mail"} size={18} strokeWidth={1.8} />
          </span>
          <p className={styles.text}>{text}</p>
          {phase === "stale" || phase === "connecting" ? (
            <button
              type="button"
              className={`btn btn-secondary ${styles.action}`}
              onClick={() => void reconnect()}
              disabled={phase === "connecting"}
            >
              {phase === "connecting" ? (signIn ? "Opening sign-in…" : "Reconnecting…") : action}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
