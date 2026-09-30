"use client";

import Link from "next/link";
import { Icon } from "@/components/Icon";
import { useOpenCount } from "@/lib/resolved-store";
import styles from "./home.module.css";

const ALL_CLEAR = "Everything is under control.";

/**
 * What still needs the owner: open questions (minus any answered in this browser) plus connections
 * that stopped syncing. A dead mailbox is never "all clear" (§47–48).
 */
function useNeeds(needsIds: string[], staleCount: number) {
  const open = useOpenCount(needsIds);
  return { open, count: open + staleCount, answeredHere: needsIds.length - open };
}

/**
 * Home's status line. It is the engine's own headline ("Action required.", "I need 2 things from
 * you.") unless something was answered in this browser since it was computed; "Everything is under
 * control." only when nothing at all is left: no question and no stale connection.
 */
export function Headline({
  greeting,
  needsIds,
  staleCount,
  headline,
}: {
  greeting: string;
  needsIds: string[];
  staleCount: number;
  headline?: string;
}) {
  const { open, count, answeredHere } = useNeeds(needsIds, staleCount);
  let text = ALL_CLEAR;
  if (count > 0) {
    text =
      headline && headline !== ALL_CLEAR && answeredHere === 0
        ? headline
        : `I need ${count === 1 ? "one thing" : `${count} things`} from you.`;
  }
  return (
    <div className={styles.headline}>
      <h1 className="display">{greeting}</h1>
      {count > 0 ? (
        <p className={styles.statusLine}>
          <Link href={open > 0 ? "/needs-you" : "/settings#connections"} className={styles.statusLink}>
            {text}
            <Icon name="arrowRight" size={22} strokeWidth={1.8} className={styles.statusArrow} />
          </Link>
        </p>
      ) : (
        <p className={styles.statusLine}>{text}</p>
      )}
    </div>
  );
}

/** The "Needs you" tile: green only when truly all clear (no question, no stale connection). */
export function NeedsTile({ needsIds, staleCount }: { needsIds: string[]; staleCount: number }) {
  const { count } = useNeeds(needsIds, staleCount);
  return (
    <Link href="/needs-you" className="card card-link tile">
      <span className="tile-label">
        <span className={`dot ${count > 0 ? "dot-attention" : "dot-good"}`} aria-hidden="true" />
        Needs you
      </span>
      <span className="tile-value num">{count}</span>
    </Link>
  );
}
