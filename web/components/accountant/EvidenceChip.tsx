"use client";

import { useState } from "react";
import { Icon, type IconName } from "@/components/Icon";
import { openEvidence } from "@/lib/api";
import type { EvidenceLink } from "@/lib/types";
import styles from "./accountant.module.css";

const ICONS: Record<NonNullable<EvidenceLink["kind"]>, IconName> = {
  payment: "payment",
  document: "document",
  email: "mail",
  letter: "document",
  file: "document",
};

/** One original the accountant can open (downloads the stored file, byte for byte). */
export function EvidenceChip({ link }: { link: EvidenceLink }) {
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = async () => {
    setOpening(true);
    setError(null);
    setError(await openEvidence(link.href));
    setOpening(false);
  };
  return (
    <span className={styles.evidenceItem}>
      <button
        type="button"
        className="chip"
        onClick={open}
        disabled={opening}
        title={link.sourceLabel ? `${link.sourceLabel}${link.filename ? ` · ${link.filename}` : ""}` : undefined}
      >
        <Icon name={ICONS[link.kind ?? "file"]} size={16} />
        <span>{opening ? "Opening…" : link.label}</span>
      </button>
      {error ? (
        <span role="alert" className={`risk-text ${styles.evidenceError}`}>
          {error}
        </span>
      ) : null}
    </span>
  );
}

export function EvidenceChips({ links, label }: { links: EvidenceLink[]; label: string }) {
  if (links.length === 0) return null;
  return (
    <ul className={styles.evidenceList} aria-label={label}>
      {links.map((l) => (
        <li key={l.id}>
          <EvidenceChip link={l} />
        </li>
      ))}
    </ul>
  );
}
