"use client";

import { useState } from "react";
import { Icon } from "@/components/Icon";
import styles from "./accountant.module.css";

type Scope = "client" | "all";

export function TeachRule({ clientName, existing }: { clientName: string; existing: string[] }) {
  const [text, setText] = useState("");
  const [scope, setScope] = useState<Scope>("client");
  const [state, setState] = useState<"idle" | "saving">("idle");
  const [rules, setRules] = useState<{ text: string; scope: Scope }[]>(existing.map((t) => ({ text: t, scope: "client" })));
  const [result, setResult] = useState<string | null>(null);

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const rule = text.trim();
    if (!rule || state === "saving") return;
    setState("saving");
    setResult(null);
    setTimeout(() => {
      setRules((r) => [{ text: rule, scope }, ...r]);
      setResult(
        scope === "client"
          ? `Done. I will apply this to ${clientName} from now on, and to 14 past transactions.`
          : "Done. I will apply this to all your clients from now on, and to 61 past transactions.",
      );
      setText("");
      setState("idle");
    }, 600);
  };

  return (
    <section className={`card ${styles.teach}`} aria-labelledby="teach-h">
      <div className="stack-1">
        <h2 id="teach-h" className="h2">
          Teach a rule
        </h2>
        <p className="muted">Write it the way you would tell a colleague.</p>
      </div>
      <form className="stack-2" onSubmit={submit}>
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
          <p className={styles.teachResult}>
            <Icon name="check" size={18} strokeWidth={2.2} />
            {result}
          </p>
        ) : null}
      </div>
      {rules.length > 0 ? (
        <div className="stack-1">
          <p className="meta">Rules for {clientName}</p>
          <ul className={styles.rules}>
            {rules.map((r, i) => (
              <li key={`${r.text}-${i}`}>
                <Icon name="bookmark" size={16} />
                <span>
                  {r.text}
                  {r.scope === "all" ? <span className="meta"> · all clients</span> : null}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
