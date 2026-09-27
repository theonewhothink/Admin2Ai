"use client";

import Link from "next/link";
import { Icon } from "@/components/Icon";
import { useOpenCount } from "@/lib/resolved-store";
import styles from "./home.module.css";

export function Headline({ greeting, needsIds }: { greeting: string; needsIds: string[] }) {
  const count = useOpenCount(needsIds);
  return (
    <div className={styles.headline}>
      <h1 className="display">{greeting}</h1>
      {count > 0 ? (
        <p className={styles.statusLine}>
          <Link href="/needs-you" className={styles.statusLink}>
            I need {count === 1 ? "one thing" : `${count} things`} from you.
            <Icon name="arrowRight" size={22} strokeWidth={1.8} className={styles.statusArrow} />
          </Link>
        </p>
      ) : (
        <p className={styles.statusLine}>Everything is under control.</p>
      )}
    </div>
  );
}

export function NeedsTile({ needsIds }: { needsIds: string[] }) {
  const count = useOpenCount(needsIds);
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
