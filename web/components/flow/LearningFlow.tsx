"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { Icon } from "@/components/Icon";
import { formatMoney, formatNumber } from "@/lib/format";
import type { LearningCounter, OneTapQuestion } from "@/lib/types";
import styles from "./flow.module.css";

const COUNT_MS = 2600;
const STAGGER_MS = 420;
const ACK_MS = 1100;

const easeOut = (t: number) => 1 - Math.pow(1 - t, 3);

/** Animated values for every counter, driven by a single animation frame loop. */
function useCounters(counters: LearningCounter[]) {
  const [values, setValues] = useState<number[]>(() => counters.map(() => 0));
  const [finished, setFinished] = useState(false);

  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const total = COUNT_MS + STAGGER_MS * (counters.length - 1);
    let raf = 0;
    let startAt: number | null = null;

    const frame = (now: number) => {
      if (startAt === null) startAt = now;
      const elapsed = reduce ? total : now - startAt;
      setValues(
        counters.map((c, i) => {
          const t = Math.min(1, Math.max(0, (elapsed - i * STAGGER_MS) / COUNT_MS));
          return Math.round(c.value * easeOut(t));
        }),
      );
      if (elapsed < total) raf = requestAnimationFrame(frame);
      else setFinished(true);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [counters]);

  return { values, finished };
}

export function LearningFlow({
  counters,
  understood,
  questions,
}: {
  counters: LearningCounter[];
  understood: number;
  questions: OneTapQuestion[];
}) {
  const { values, finished } = useCounters(counters);
  const [index, setIndex] = useState(0);
  const [ack, setAck] = useState(false);
  const allDone = index >= questions.length;
  const summaryRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    if (finished) summaryRef.current?.focus();
  }, [finished]);

  useEffect(() => {
    if (!ack) return;
    const t = setTimeout(() => {
      setAck(false);
      setIndex((i) => i + 1);
    }, ACK_MS);
    return () => clearTimeout(t);
  }, [ack]);

  const q = questions[index];

  return (
    <div className={styles.learning}>
      <div className={styles.stepHead}>
        <h1 className="h1" aria-live="polite">
          {finished ? "Done reading." : "Learning how your business works…"}
        </h1>
        {!finished ? <p className="lead">Reading your email and bank history. This takes a few minutes.</p> : null}
      </div>

      <ul className={`card ${styles.counters}`}>
        {counters.map((c, i) => {
          const v = values[i] ?? 0;
          const done = v === c.value;
          return (
            <li key={c.id} className={styles.counter} data-done={done}>
              <span className={styles.counterSource}>{c.source}</span>
              <span className={styles.counterValue}>
                <span className="num">{formatNumber(v)}</span>
                <span className={styles.counterLabel}>{c.label}</span>
              </span>
              <span className={styles.counterState} aria-hidden="true">
                {done ? <Icon name="check" size={16} strokeWidth={2.2} /> : <span className={styles.pulse} />}
              </span>
            </li>
          );
        })}
      </ul>

      {finished ? (
        <section className={styles.understood} aria-labelledby="understood-h">
          <h2 id="understood-h" ref={summaryRef} tabIndex={-1} className={styles.understoodTitle}>
            We understand <span className="num">{understood}%</span> of your business.
          </h2>

          {!allDone && q ? (
            <>
              <p className="lead">
                We need you to confirm {questions.length} things.{" "}
                <span className="num">
                  ({index + 1} of {questions.length})
                </span>
              </p>
              <div key={q.id} className={`card ${styles.question}`} data-ack={ack}>
                {ack ? (
                  <p className={styles.ack} role="status">
                    <Icon name="check" size={18} strokeWidth={2.2} />I will remember this.
                  </p>
                ) : (
                  <>
                    <div className={styles.questionHead}>
                      <span className="h3">{q.subject}</span>
                      <span className="num muted">
                        {formatMoney(q.amount, q.currency)} {q.cadence}
                      </span>
                    </div>
                    <p className="muted">What is this?</p>
                    <div className={styles.taps}>
                      {q.options.map((o) => (
                        <button key={o.id} type="button" className="btn btn-secondary" onClick={() => setAck(true)}>
                          {o.label}
                        </button>
                      ))}
                    </div>
                  </>
                )}
              </div>
            </>
          ) : (
            <div className={`card ${styles.question} ${styles.final}`}>
              <p className={styles.ack}>
                <Icon name="check" size={18} strokeWidth={2.2} />
                Done. I will take it from here.
              </p>
              <Link href="/" className="btn btn-primary btn-lg">
                Go to Home
              </Link>
            </div>
          )}
        </section>
      ) : null}
    </div>
  );
}
