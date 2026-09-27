"use client";

import { useEffect, useState } from "react";
import { formatDayShort, formatTime } from "@/lib/format";
import type { Connection } from "@/lib/types";
import { Icon } from "./Icon";
import styles from "./ConnectionBanner.module.css";

type Phase = "stale" | "connecting" | "done" | "gone";

const what: Record<Connection["kind"], string> = {
  email: "Your email",
  bank: "Your bank",
  accountant: "Your accountant’s inbox",
};

export function ConnectionBanner({ connection }: { connection: Connection }) {
  const [phase, setPhase] = useState<Phase>("stale");

  useEffect(() => {
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
            </p>
          )}
          {phase === "stale" || phase === "connecting" ? (
            <button
              type="button"
              className={`btn btn-secondary ${styles.action}`}
              onClick={() => setPhase("connecting")}
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
