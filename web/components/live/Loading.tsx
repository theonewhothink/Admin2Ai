"use client";

import { useEffect, useState, useSyncExternalStore } from "react";
import { engineState, subscribeEngine } from "@/lib/engine";
import styles from "./live.module.css";

const IDLE = { phase: "idle" } as const;
const idle = () => IDLE;

/** Calm placeholder while the in-browser engine starts or a page loads its data. */
export function Loading({ narrow = true }: { narrow?: boolean }) {
  const engine = useSyncExternalStore(subscribeEngine, engineState, idle);
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const t = window.setTimeout(() => setSlow(true), 2500);
    return () => window.clearTimeout(t);
  }, []);

  const failed = engine.phase === "failed";
  return (
    <div className={`${narrow ? "container-narrow" : "container"} page`}>
      <div className={`card card-pad ${styles.loading}`} role="status" aria-live="polite">
        {failed ? null : <span className={styles.pulse} aria-hidden="true" />}
        <p className="h3">{failed ? "I couldn’t get ready." : "Getting everything ready…"}</p>
        <p className="muted">
          {failed
            ? "Reload the page to try again."
            : slow && engine.phase === "starting"
              ? "The first visit takes a few seconds. After that it is quick."
              : "One moment."}
        </p>
      </div>
    </div>
  );
}
