"use client";

import Link from "next/link";
import { useCallback } from "react";
import { Icon } from "@/components/Icon";
import { countWord } from "@/lib/format";
import { markAnswered, useAnswered } from "@/lib/resolved-store";
import type { NeedsYouItem } from "@/lib/types";
import { DecisionCard } from "./DecisionCard";
import styles from "./needs.module.css";

export function NeedsYouList({ items, companyNames }: { items: NeedsYouItem[]; companyNames: Record<string, string> }) {
  const answered = useAnswered();
  const open = items.filter((i) => !answered.has(i.id));
  const resolve = useCallback((id: string) => markAnswered(id), []);

  const lead =
    open.length === 0
      ? "Nothing needs you right now."
      : open.length === 1
        ? "I still need one thing. It takes a few seconds."
        : `${countWord(open.length).replace(/^./, (c) => c.toUpperCase())} things. Each takes a few seconds.`;

  return (
    <>
      <header className="page-head">
        <h1 className="h1">Needs you</h1>
        <p className="lead" aria-live="polite">
          {lead}
        </p>
      </header>

      {open.length > 0 ? (
        <div className={styles.list}>
          {open.map((item) => (
            <DecisionCard
              key={item.id}
              item={item}
              companyName={item.companyId ? companyNames[item.companyId] : undefined}
              onResolved={() => resolve(item.id)}
            />
          ))}
        </div>
      ) : (
        <div className={`card ${styles.empty}`}>
          <span className={styles.emptyIcon}>
            <Icon name="check" size={26} strokeWidth={2} />
          </span>
          <p className="h2">All clear.</p>
          <p className="muted">I will let you know when something needs you.</p>
          <Link href="/" className="btn btn-secondary">
            Back to Home
          </Link>
        </div>
      )}
    </>
  );
}
