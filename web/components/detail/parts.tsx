"use client";

import Link from "next/link";
import { useState, type ReactNode } from "react";
import { Icon, type IconName } from "@/components/Icon";
import { Loading } from "@/components/live/Loading";
import { openOriginal } from "@/lib/api";
import { formatDay, formatMoney, formatTime, localDay } from "@/lib/format";
import type { ChainStep, EvidenceRef, HistoryStep, ImportChain, Tone } from "@/lib/types";
import styles from "./detail.module.css";

/** "← Back to …" above a detail page. */
export function BackLink({ href, label }: { href: string; label: string }) {
  return (
    <div className={styles.back}>
      <Link href={href} className="link-quiet">
        <Icon name="chevronLeft" size={16} />
        {label}
      </Link>
    </div>
  );
}

/** Loading, not found, or the API's own plain message instead of the page. */
export function PageState({
  loading,
  error,
  status,
  what,
  back,
}: {
  loading: boolean;
  error: string | null;
  status: number;
  what: string;
  back?: { href: string; label: string };
}) {
  if (loading) return <Loading />;
  return (
    <div className="container-narrow page">
      {back ? <BackLink href={back.href} label={back.label} /> : null}
      <div className="card card-pad stack-2" role="status">
        <p className="h3">{status === 404 ? `I couldn’t find that ${what}.` : "This page didn’t load."}</p>
        {status !== 404 && error ? <p className="muted">{error}</p> : null}
      </div>
    </div>
  );
}

/** The engine's stage as a calm pill: green only when it is really closed, red only for a real conflict. */
export function stageTone(stage: string): Tone {
  if (stage === "closed" || stage === "not_required") return "good";
  if (stage === "conflict") return "risk";
  if (stage === "needs_owner") return "attention";
  return "neutral";
}

export function Pill({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`pill${tone === "neutral" ? "" : ` pill-${tone}`}`}>{children}</span>;
}

export function money(amount: number | null | undefined, currency = "EUR"): string {
  return amount === null || amount === undefined ? "" : formatMoney(amount, currency);
}

/** "29 September", or nothing. */
export function day(iso: string | null | undefined): string {
  return iso ? formatDay(iso) : "";
}

/** A labelled section with an optional short line, or an action, on the right. */
export function Section({
  id,
  title,
  aside,
  action,
  children,
}: {
  id: string;
  title: string;
  aside?: ReactNode;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section aria-labelledby={`${id}-h`} className={styles.section} id={id}>
      <div className="section-head">
        <h2 id={`${id}-h`} className="h2">
          {title}
        </h2>
        {action ?? (aside ? <span className="meta">{aside}</span> : null)}
      </div>
      {children}
    </section>
  );
}

/** Label / value pairs, two columns on wider screens. */
export function Facts({ items }: { items: { label: string; value: ReactNode }[] }) {
  const shown = items.filter((i) => i.value !== null && i.value !== undefined && i.value !== "");
  if (shown.length === 0) return null;
  return (
    <dl className={styles.facts}>
      {shown.map((f) => (
        <div key={f.label} className={styles.fact}>
          <dt>{f.label}</dt>
          <dd>{f.value}</dd>
        </div>
      ))}
    </dl>
  );
}

const ORIGINAL_ICON = (label: string): IconName =>
  /transfer|debit|card payment|payment|paid|bank/i.test(label) ? "payment" : /email|message/i.test(label) ? "mail" : "document";

/** One original: opens (downloads) the stored file, byte for byte. */
export function OriginalChip({ evidence }: { evidence: EvidenceRef }) {
  const [opening, setOpening] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const open = async () => {
    setOpening(true);
    setError(null);
    setError(await openOriginal(evidence.id));
    setOpening(false);
  };
  return (
    <span className={styles.chipWrap}>
      <button type="button" className="chip" onClick={open} disabled={opening} aria-label={`Open the original: ${evidence.label}`}>
        <Icon name={ORIGINAL_ICON(evidence.label)} size={16} />
        <span className={styles.chipText}>{opening ? "Opening…" : evidence.label}</span>
      </button>
      {error ? (
        <span role="alert" className={`risk-text ${styles.chipError}`}>
          {error}
        </span>
      ) : null}
    </span>
  );
}

/** The originals behind something (§54): every amount links to its proof. */
export function Originals({ items, label = "Originals" }: { items: EvidenceRef[]; label?: string }) {
  const unique = items.filter((e, i) => e && e.id && items.findIndex((x) => x.id === e.id) === i);
  if (unique.length === 0) return null;
  return (
    <ul className={styles.chips} aria-label={label}>
      {unique.map((e) => (
        <li key={e.id}>
          <OriginalChip evidence={e} />
        </li>
      ))}
    </ul>
  );
}

/** Evidence ids without a label of their own (a payment's bank line, a deadline's letter). */
export function OriginalIds({ ids, label }: { ids: string[]; label: (index: number) => string }) {
  return <Originals items={ids.map((id, i) => ({ id, label: label(i) }))} />;
}

const DOCUMENT_STEPS = new Set(["invoice", "credit_note", "advance_invoice"]);

/** Where one step of a chain opens: its document or its payment (held back / kept parts have no page). */
export function chainHref(step: ChainStep): string | null {
  if (step.id.includes(":")) return null;
  return DOCUMENT_STEPS.has(step.step) ? documentHref(step.id) : paymentHref(step.id);
}

export function documentHref(id: string): string {
  return `/documents/detail?id=${encodeURIComponent(id)}`;
}

export function paymentHref(id: string): string {
  return `/payments/detail?id=${encodeURIComponent(id)}`;
}

/** Invoice → payment → credit note → refund, or deposit → invoice → each part → held back. */
export function ChainList({ steps, current, label }: { steps: ChainStep[]; current: string; label: string }) {
  if (steps.length === 0) return null;
  return (
    <ol className={`card ${styles.chain}`} aria-label={label}>
      {steps.map((s) => {
        const href = s.id === current ? null : chainHref(s);
        const inner = (
          <>
            <span className={styles.chainMain}>
              <span className={styles.rowTitle}>{s.label}</span>
              {s.date ? <span className="meta">{formatDay(s.date)}</span> : null}
            </span>
            <span className={`num ${styles.rowAmount}`}>{money(s.amount, s.currency)}</span>
            {href ? <Icon name="chevronRight" size={18} className={styles.chevron} /> : null}
          </>
        );
        return (
          <li key={`${s.step}-${s.id}`} className={styles.chainItem} aria-current={s.id === current ? "step" : undefined}>
            {href ? (
              <Link href={href} className={styles.chainRow}>
                {inner}
              </Link>
            ) : (
              <div className={styles.chainRow}>{inner}</div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/** An import purchase: its order, customs and duties, linked by reference. */
export function ImportChainView({ chain }: { chain: ImportChain }) {
  return (
    <div className="card card-pad stack-2">
      <p>{chain.line}</p>
      <ul className={styles.plainList}>
        {chain.pieces.map((p) => {
          const href = p.kind === "document" ? documentHref(p.id) : p.kind === "transaction" ? paymentHref(p.id) : null;
          return (
            <li key={`${p.kind}-${p.id}`}>
              {href ? (
                <Link href={href} className="link">
                  {p.text}
                </Link>
              ) : (
                p.text
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** What happened to it, step by step (§55). */
export function Timeline({ steps }: { steps: HistoryStep[] }) {
  if (steps.length === 0) return null;
  return (
    <ol className={styles.timeline}>
      {steps.map((s, i) => (
        <li key={`${s.stage}-${i}`}>
          <span className={styles.timelineDot} data-stage={s.stage} aria-hidden="true" />
          <span className={styles.timelineMain}>
            <span className={styles.rowTitle}>{s.label || s.stage}</span>
            {s.note ? <span className="muted">{s.note}</span> : null}
          </span>
          <span className="meta num">
            {formatDay(localDay(s.at))} · {formatTime(s.at)}
          </span>
        </li>
      ))}
    </ol>
  );
}

/** A plain one-line result of a change, read out to screen readers. */
export function Result({ result }: { result: { ok: boolean; text: string } | null }) {
  return (
    <p className={result ? (result.ok ? styles.resultOk : styles.resultFail) : styles.resultEmpty} role="status" aria-live="polite">
      {result?.text ?? ""}
    </p>
  );
}
