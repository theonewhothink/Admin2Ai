"use client";

import detail from "@/components/detail/detail.module.css";
import { formatMoney } from "@/lib/format";
import type { SplitOffer } from "@/lib/types";
import styles from "./needs.module.css";

export interface SplitDraft {
  mode: "amount" | "percent";
  /** What the owner typed for each cost center, as typed. */
  values: Record<string, string>;
}

const NUMBER = /^\d+(?:[.,]\d{1,2})?$/;

function parse(value: string | undefined): number | null {
  const v = (value ?? "").trim();
  if (!NUMBER.test(v)) return null;
  return Number(v.replace(",", "."));
}

/** Every share has a number. Whether they add up exactly is the engine's call: it says so plainly. */
export function splitReady(split: SplitOffer, draft: SplitDraft): boolean {
  return split.costCenters.every((c) => {
    const n = parse(draft.values[c.id]);
    return n !== null && n > 0;
  });
}

/**
 * "Split it between several": an amount or a percentage for each job, property or vehicle. The running
 * total shows what is left; the engine checks that the parts add up to the cent and answers in plain words.
 */
export function SplitEditor({
  split,
  currency,
  draft,
  onChange,
  itemId,
}: {
  split: SplitOffer;
  currency: string;
  draft: SplitDraft;
  onChange: (draft: SplitDraft) => void;
  itemId: string;
}) {
  const percent = draft.mode === "percent";
  const target = percent ? 100 : split.total;
  const cents = split.costCenters.reduce((sum, c) => sum + Math.round((parse(draft.values[c.id]) ?? 0) * 100), 0);
  const left = Math.round(target * 100) - cents;
  const show = (n: number) => (percent ? `${(n / 100).toLocaleString("en-GB", { maximumFractionDigits: 2 })}%` : formatMoney(n / 100, currency));
  const symbol = formatMoney(0, currency).replace(/[\d.,\s]/g, "") || currency;
  const hintId = `${itemId}-split-hint`;
  const totalId = `${itemId}-split-total`;

  return (
    <div className={styles.split}>
      <div className={styles.splitHead}>
        <div className="segmented" role="group" aria-label="Split by">
          <button type="button" aria-pressed={!percent} onClick={() => onChange({ mode: "amount", values: {} })}>
            Amounts
          </button>
          <button type="button" aria-pressed={percent} onClick={() => onChange({ mode: "percent", values: {} })}>
            Percentages
          </button>
        </div>
      </div>
      <p id={hintId} className="meta">
        {split.hint}
      </p>
      <div className={detail.splitRows}>
        {split.costCenters.map((c) => {
          const id = `${itemId}-split-${c.id}`;
          return (
            <div key={c.id} className={detail.splitRow}>
              <label htmlFor={id} className={styles.splitLabel}>
                {c.label}
              </label>
              <span className={detail.splitInput}>
                <input
                  id={id}
                  className="input num"
                  inputMode="decimal"
                  autoComplete="off"
                  placeholder="0"
                  aria-describedby={`${hintId} ${totalId}`}
                  value={draft.values[c.id] ?? ""}
                  onChange={(e) => onChange({ ...draft, values: { ...draft.values, [c.id]: e.target.value } })}
                />
                <span className={detail.splitUnit} aria-hidden="true">
                  {percent ? "%" : symbol}
                </span>
              </span>
            </div>
          );
        })}
      </div>
      <p id={totalId} className={detail.splitTotal} aria-live="polite">
        <span>{percent ? "Total" : `Total of ${formatMoney(split.total, currency)}`}</span>
        <span className={`num ${left === 0 ? "" : "attention-text"}`}>
          {left === 0 ? "Adds up exactly" : left > 0 ? `${show(left)} still to place` : `${show(-left)} too much`}
        </span>
      </p>
    </div>
  );
}
