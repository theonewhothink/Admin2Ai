"use client";

import { useEffect, useId, useState } from "react";
import { Icon } from "@/components/Icon";
import { Bullets, Disclosure } from "@/components/ui";
import { answerNeedsYou } from "@/lib/api";
import { formatDay, formatMoney } from "@/lib/format";
import type { NeedsYouApprovalItem, NeedsYouChoiceItem, NeedsYouItem } from "@/lib/types";
import styles from "./needs.module.css";

type Phase = "open" | "sending" | "done" | "leaving";

const HOLD_MS = 1600;
const LEAVE_MS = 240;

/**
 * Handles the done → leaving → removed sequence shared by both card kinds. With `serverMessage`, the
 * engine's own reply (what really happened, e.g. whether the request to the supplier went out) is
 * shown instead of the card's preset text whenever there is one.
 */
function useResolution(onResolved: () => void, serverMessage = false) {
  const [phase, setPhase] = useState<Phase>("open");
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (phase === "done") {
      const t = setTimeout(() => setPhase("leaving"), HOLD_MS);
      return () => clearTimeout(t);
    }
    if (phase === "leaving") {
      const t = setTimeout(onResolved, LEAVE_MS);
      return () => clearTimeout(t);
    }
  }, [phase, onResolved]);

  const submit = async (id: string, optionId: string, remember: boolean, doneMessage: string) => {
    setFailed(false);
    setPhase("sending");
    const res = await answerNeedsYou(id, optionId, remember);
    if (res.ok) {
      setMessage(serverMessage && res.message ? res.message : doneMessage);
      setPhase("done");
    } else {
      setFailed(true);
      setPhase("open");
    }
  };

  return { phase, message, failed, submit };
}

interface CardProps<T extends NeedsYouItem> {
  item: T;
  companyName?: string;
  onResolved: () => void;
}

export function DecisionCard({ item, companyName, onResolved }: CardProps<NeedsYouItem>) {
  return item.kind === "choice" ? (
    <ChoiceCard item={item} companyName={companyName} onResolved={onResolved} />
  ) : (
    <ApprovalCard item={item} companyName={companyName} onResolved={onResolved} />
  );
}

function Shell({
  item,
  phase,
  message,
  children,
}: {
  item: NeedsYouItem;
  phase: Phase;
  message: string;
  children: React.ReactNode;
}) {
  const resolved = phase === "done" || phase === "leaving";
  return (
    <article id={item.id} className={styles.outer} data-phase={phase} aria-labelledby={`${item.id}-title`}>
      <div className={styles.clip}>
        <div className={`card ${styles.card}`} data-tone={item.tone}>
          <div className={styles.bodyWrap}>
            <div className={styles.body} aria-hidden={resolved} inert={resolved}>
              {children}
            </div>
          </div>
          <div className={styles.doneWrap}>
            <div className={styles.done} role="status" aria-live="polite">
              {resolved ? (
                <>
                  <span className={styles.doneIcon}>
                    <Icon name="check" size={20} strokeWidth={2.2} />
                  </span>
                  <span className={styles.doneText}>{message}</span>
                </>
              ) : null}
            </div>
          </div>
        </div>
      </div>
    </article>
  );
}

function Header({ item, companyName, detail }: { item: NeedsYouItem; companyName?: string; detail?: string }) {
  return (
    <header className={styles.header}>
      <div className={styles.eyebrow} data-tone={item.tone}>
        <span className={`dot dot-${item.tone}`} aria-hidden="true" />
        {item.eyebrow}
      </div>
      <div className={styles.titleRow}>
        <h2 id={`${item.id}-title`} className={styles.merchant}>
          {item.merchant}
        </h2>
        <div className={`${styles.amount} num`}>{formatMoney(item.amount, item.currency)}</div>
      </div>
      <p className="meta">
        {formatDay(item.date)}
        {detail ? ` · ${detail}` : ""}
        {companyName ? ` · ${companyName}` : ""}
      </p>
    </header>
  );
}

function WhyBlock({ why }: { why: string[] }) {
  return (
    <Disclosure summary="Why am I seeing this?" className={styles.why}>
      <Bullets items={why} />
    </Disclosure>
  );
}

/* ---------- Choice: which company does this belong to? ---------- */

function ChoiceCard({ item, companyName, onResolved }: CardProps<NeedsYouChoiceItem>) {
  const groupName = useId();
  const [optionId, setOptionId] = useState<string | null>(null);
  const [subId, setSubId] = useState<string | null>(null);
  const [remember, setRemember] = useState(item.remember?.defaultChecked ?? true);
  const { phase, message, failed, submit } = useResolution(onResolved);

  const option = item.options.find((o) => o.id === optionId) ?? null;
  const sub = option?.choices?.find((c) => c.id === subId) ?? null;
  const needsSub = Boolean(option?.choices?.length);
  const ready = option !== null && (!needsSub || sub !== null);
  const finalId = sub?.id ?? option?.id ?? "";
  const finalLabel = sub?.label ?? option?.label ?? "";

  const rememberText = (() => {
    if (!item.remember || !ready) return "";
    const override = item.remember.overrides?.[finalId];
    return override ?? item.remember.template.replace("{choice}", finalLabel);
  })();

  const confirm = () => {
    if (!ready || phase !== "open") return;
    const keep = Boolean(item.remember) && remember;
    void submit(item.id, finalId, keep, keep ? "Done. I will remember this." : "Done.");
  };

  return (
    <Shell item={item} phase={phase} message={message}>
      <Header item={item} companyName={companyName} detail={item.paidWith ? `paid with ${item.paidWith}` : undefined} />

      <fieldset className={styles.fieldset}>
        <legend className={styles.question}>{item.question}</legend>
        <div className={styles.options}>
          {item.options.map((o) => (
            <div key={o.id} className={styles.optionWrap}>
              <label className="choice">
                <input
                  type="radio"
                  name={groupName}
                  value={o.id}
                  checked={optionId === o.id}
                  onChange={() => {
                    setOptionId(o.id);
                    setSubId(null);
                  }}
                />
                <span className="radio-mark" aria-hidden="true" />
                {o.label}
              </label>
              {o.choices && optionId === o.id ? (
                <div className={styles.subChoices} role="radiogroup" aria-label="Which company?">
                  {o.choices.map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      role="radio"
                      aria-checked={subId === c.id}
                      className={styles.subChoice}
                      onClick={() => setSubId(c.id)}
                    >
                      {c.label}
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </fieldset>

      <div className={styles.confirmArea} data-visible={ready}>
        <div className={styles.confirmInner}>
          {item.remember && ready ? (
            <label className="checkbox">
              <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
              <span>{rememberText}</span>
            </label>
          ) : null}
          <div className={styles.actions}>
            <button type="button" className="btn btn-primary" onClick={confirm} disabled={!ready || phase !== "open"}>
              {phase === "sending" ? "One moment…" : "Confirm"}
            </button>
          </div>
        </div>
      </div>

      {failed ? <p className={styles.retry}>I couldn’t save that just now. Please try again in a moment.</p> : null}

      <WhyBlock why={item.why} />
    </Shell>
  );
}

/* ---------- Approval: a payment that stays blocked until the owner verifies ---------- */

function ApprovalCard({ item, companyName, onResolved }: CardProps<NeedsYouApprovalItem>) {
  const [verifying, setVerifying] = useState(false);
  const [called, setCalled] = useState(false);
  const { phase, message, failed, submit } = useResolution(onResolved, true);
  const busy = phase !== "open";

  const keepBlocked = () => void submit(item.id, item.keepBlocked.optionId, false, item.keepBlocked.message);
  const release = () => {
    if (!called) return;
    void submit(item.id, item.verification.confirmOptionId, false, item.verification.confirmedMessage);
  };

  return (
    <Shell item={item} phase={phase} message={message}>
      <Header item={item} companyName={companyName} />

      <div className={styles.approvalText}>
        <p className={styles.approvalTitle}>{item.title}</p>
        <p className="muted">{item.body}</p>
      </div>

      <dl className={styles.facts}>
        {item.facts.map((f) => (
          <div key={f.label} className={styles.fact}>
            <dt>{f.label}</dt>
            <dd className={`num ${f.tone === "risk" ? "risk-text" : ""}`}>{f.value}</dd>
          </div>
        ))}
      </dl>

      {!verifying ? (
        <div className={styles.actions}>
          <button type="button" className="btn btn-primary" onClick={() => setVerifying(true)} disabled={busy}>
            <Icon name="phone" size={18} />
            {item.verification.optionLabel}
          </button>
          <button type="button" className="btn btn-secondary" onClick={keepBlocked} disabled={busy}>
            <Icon name="lock" size={18} />
            {phase === "sending" ? "One moment…" : item.keepBlocked.label}
          </button>
        </div>
      ) : (
        <div className={styles.verify}>
          <div className={styles.verifyStep}>
            <span className={styles.verifyIcon}>
              <Icon name="phone" size={18} />
            </span>
            <p>{item.verification.instruction}</p>
          </div>
          <label className="checkbox">
            <input type="checkbox" checked={called} onChange={(e) => setCalled(e.target.checked)} />
            <span>{item.verification.checkboxLabel}</span>
          </label>
          <div className={styles.actions}>
            <button type="button" className="btn btn-primary" onClick={release} disabled={!called || busy}>
              {phase === "sending" ? "One moment…" : item.verification.confirmLabel}
            </button>
            <button type="button" className="btn btn-secondary" onClick={keepBlocked} disabled={busy}>
              {item.keepBlocked.label}
            </button>
          </div>
        </div>
      )}

      {failed ? <p className={styles.retry}>I couldn’t save that just now. Please try again in a moment.</p> : null}

      <WhyBlock why={item.why} />
    </Shell>
  );
}
