"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { AskBox } from "@/components/AskBox";
import { Icon, type IconName } from "@/components/Icon";
import { ask } from "@/lib/api";
import { evidenceHref, evidenceKind, type EvidenceKind } from "@/lib/evidence";
import type { AskAnswer, Evidence } from "@/lib/types";
import styles from "./ask.module.css";

interface Turn {
  id: number;
  question: string;
  answer: AskAnswer | null;
}

const evidenceIcon: Record<EvidenceKind, IconName> = {
  document: "document",
  payment: "payment",
  month: "building",
  decision: "needs",
  other: "link",
};

function EvidenceChip({ e }: { e: Evidence }) {
  const href = evidenceHref(e);
  const icon = <Icon name={evidenceIcon[evidenceKind(e)]} size={16} />;
  return href ? (
    <Link href={href} className="chip">
      {icon}
      {e.label}
    </Link>
  ) : (
    <span className="chip">
      {icon}
      {e.label}
    </span>
  );
}

export function AskClient({ initialQuestion, examples }: { initialQuestion?: string; examples: string[] }) {
  const [value, setValue] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const nextId = useRef(1);
  const askedInitial = useRef(false);
  const busy = turns.some((t) => t.answer === null);

  const run = useCallback((question: string) => {
    const id = nextId.current++;
    setTurns((prev) => [{ id, question, answer: null }, ...prev]);
    setValue("");
    void ask(question).then((answer) => {
      setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, answer } : t)));
    });
  }, []);

  // Answer the question passed in the URL (from the Home command box) once.
  useEffect(() => {
    if (initialQuestion && !askedInitial.current) {
      askedInitial.current = true;
      run(initialQuestion);
    }
  }, [initialQuestion, run]);

  return (
    <div className="stack-4">
      <AskBox onAsk={run} value={value} onValueChange={setValue} busy={busy} autoFocus={!initialQuestion} />

      {turns.length === 0 ? (
        <section aria-labelledby="examples-h" className="stack-2">
          <h2 id="examples-h" className="meta">
            You could ask
          </h2>
          <ul className={styles.examples}>
            {examples.map((q) => (
              <li key={q}>
                <button type="button" className={styles.example} onClick={() => run(q)}>
                  {q}
                  <Icon name="arrowRight" size={16} />
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : (
        <div className={styles.turns} aria-live="polite">
          {turns.map((t) => (
            <article key={t.id} className={`card ${styles.turn}`}>
              <p className={styles.question}>{t.question}</p>
              {t.answer ? (
                <div className={styles.answer}>
                  <p className={styles.answerText}>{t.answer.answer}</p>
                  {t.answer.evidence.length > 0 ? (
                    <div className="stack-1">
                      <p className="meta">Based on</p>
                      <ul className={styles.evidence}>
                        {t.answer.evidence.map((e) => (
                          <li key={e.id}>
                            <EvidenceChip e={e} />
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                </div>
              ) : (
                <p className={styles.thinking}>Looking through your email, bank and documents…</p>
              )}
            </article>
          ))}

          <section aria-labelledby="more-h" className="stack-2">
            <h2 id="more-h" className="meta">
              You could also ask
            </h2>
            <ul className={styles.moreExamples}>
              {examples
                .filter((q) => !turns.some((t) => t.question === q))
                .slice(0, 4)
                .map((q) => (
                  <li key={q}>
                    <button type="button" className="chip" onClick={() => run(q)} disabled={busy}>
                      {q}
                    </button>
                  </li>
                ))}
            </ul>
          </section>
        </div>
      )}
    </div>
  );
}
