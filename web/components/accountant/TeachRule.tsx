"use client";

import { useState } from "react";
import { Icon } from "@/components/Icon";
import { call } from "@/lib/api";
import styles from "./accountant.module.css";

type Scope = "client" | "all";

interface RuleReply {
  ok?: boolean;
  message?: string;
  affected?: number;
  rule?: { id: string; label: string; scope: string };
}

/**
 * Teach a rule (§28). It is saved by the engine (POST /api/accountant/rules), which answers with the
 * rule as it understood it and how many payments it applies to so far. What is shown is that answer,
 * or the engine's own reason for refusing it; nothing is assumed.
 */
export function TeachRule({ clientName, existing }: { clientName: string; existing: { label: string; scope: string }[] }) {
  const [text, setText] = useState("");
  const [scope, setScope] = useState<Scope>("client");
  const [state, setState] = useState<"idle" | "saving">("idle");
  const [rules, setRules] = useState<{ label: string; scope: string }[]>(existing);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const rule = text.trim();
    if (!rule || state === "saving") return;
    setState("saving");
    setResult(null);
    const r = await call<RuleReply>("POST", "/api/accountant/rules", { text: rule, scope });
    setState("idle");
    if (!r.ok || !r.body.rule) {
      setResult({ ok: false, text: r.body.message ?? "I couldn’t save that rule. Try again." });
      return;
    }
    const saved = r.body.rule;
    setRules((list) => [{ label: saved.label, scope: saved.scope }, ...list.filter((x) => x.label !== saved.label)]);
    setResult({ ok: true, text: r.body.message ?? `Done. ${saved.label}.` });
    setText("");
  };

  return (
    <section className={`card ${styles.teach}`} aria-labelledby="teach-h">
      <div className="stack-1">
        <h2 id="teach-h" className="h2">
          Teach a rule
        </h2>
        <p className="muted">Write it the way you would tell a colleague.</p>
      </div>
      <form className="stack-2" onSubmit={(e) => void submit(e)}>
        <label htmlFor="rule-text" className="visually-hidden">
          Rule
        </label>
        <input
          id="rule-text"
          className="input"
          placeholder="Treat all Adobe subscriptions as Software"
          value={text}
          onChange={(e) => setText(e.target.value)}
          autoComplete="off"
        />
        <div className={styles.teachRow}>
          <div className="segmented" role="group" aria-label="Apply to">
            <button type="button" aria-pressed={scope === "client"} onClick={() => setScope("client")}>
              This client
            </button>
            <button type="button" aria-pressed={scope === "all"} onClick={() => setScope("all")}>
              All clients
            </button>
          </div>
          <button type="submit" className="btn btn-primary" disabled={!text.trim() || state === "saving"}>
            {state === "saving" ? "One moment…" : "Teach"}
          </button>
        </div>
      </form>
      <div aria-live="polite">
        {result ? (
          result.ok ? (
            <p className={styles.teachResult}>
              <Icon name="check" size={18} strokeWidth={2.2} />
              {result.text}
            </p>
          ) : (
            <p className="meta" role="alert">
              {result.text}
            </p>
          )
        ) : null}
      </div>
      {rules.length > 0 ? (
        <div className="stack-1">
          <p className="meta">Rules for {clientName}</p>
          <ul className={styles.rules}>
            {rules.map((r, i) => (
              <li key={`${r.label}-${i}`}>
                <Icon name="bookmark" size={16} />
                <span>
                  {r.label}
                  {r.scope.startsWith("all") ? <span className="meta"> · all clients</span> : null}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
