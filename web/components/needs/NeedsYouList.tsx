"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Icon } from "@/components/Icon";
import { getNeedsYou } from "@/lib/api";
import { countWord } from "@/lib/format";
import { forgetAnswered, markAnswered, useAnswered } from "@/lib/resolved-store";
import type { NeedsYouItem } from "@/lib/types";
import { DecisionCard } from "./DecisionCard";
import styles from "./needs.module.css";

export function NeedsYouList({ items, companyNames }: { items: NeedsYouItem[]; companyNames: Record<string, string> }) {
  const answered = useAnswered();
  // The list as read again after a sign-in code went through; until then, the one the page loaded.
  const [fresh, setFresh] = useState<NeedsYouItem[] | null>(null);
  const list = fresh ?? items;
  const open = list.filter((i) => !answered.has(i.id));

  // A website asks for a new sign-in code under the same id: whenever the engine lists one, it is open.
  useEffect(() => {
    forgetAnswered(list.filter((i) => i.kind === "code").map((i) => i.id));
  }, [list]);

  const resolve = useCallback((item: NeedsYouItem) => {
    markAnswered(item.id);
    // The invoices the website gave may raise questions of their own: read the list again.
    if (item.kind === "code") void getNeedsYou().then(setFresh, () => undefined);
  }, []);

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
              onResolved={() => resolve(item)}
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
