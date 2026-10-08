"use client";

import Link from "next/link";
import { useMemo, useState } from "react";
import { Icon } from "@/components/Icon";
import { formatDayShort, formatMoney, formatTime, localDay } from "@/lib/format";
import type { Pipeline, PipelineItem, PipelineStage } from "@/lib/types";
import styles from "./diagram.module.css";

/**
 * What the system is doing, drawn from the engine's own records
 * (GET /api/pipeline): where items come from, the eight steps every item
 * takes, where items step aside, where they end up, and each item's journey
 * with the agent that took every step.
 */

type Filter = { kind: "open" | "waiting" | "closed" | "all" } | { kind: "stage"; stage: PipelineStage };

const SIDE_TONE: Record<string, string> = { needs_owner: "attention", conflict: "risk", not_required: "muted" };

function money(item: PipelineItem) {
  return item.amount === null ? "" : formatMoney(item.amount, item.currency || "EUR");
}

function when(at: string) {
  return `${formatDayShort(localDay(at))} · ${formatTime(at)}`;
}

function Track({ item, stages }: { item: PipelineItem; stages: PipelineStage[] }) {
  const reached = new Set(["discovered", ...item.journey.map((j) => j.stage)]);
  const closed = item.stage === "closed";
  return (
    <ol className={styles.dots} aria-label={`Steps done: ${stages.filter((s) => reached.has(s.id)).map((s) => s.label).join(", ")}`}>
      {stages.map((s) => {
        const state = item.stage === s.id ? "current" : reached.has(s.id) ? "done" : "todo";
        return (
          <li key={s.id} className={styles.dot} data-state={state} data-closed={closed || undefined} title={s.label}>
            <span className="visually-hidden">{s.label}</span>
          </li>
        );
      })}
    </ol>
  );
}

function ItemRow({ item, stages }: { item: PipelineItem; stages: PipelineStage[] }) {
  const tone = SIDE_TONE[item.stage];
  return (
    <li className={`card ${styles.item}`}>
      <div className={styles.itemHead}>
        <Icon name={item.kind === "payment" ? "payment" : "document"} size={20} style={{ color: "var(--text-2)", flexShrink: 0 }} />
        <div className={styles.itemText}>
          <strong>{item.title}</strong>
          <span className="meta">{[item.detail, item.company, item.date ? formatDayShort(item.date) : null].filter(Boolean).join(" · ")}</span>
        </div>
        <span className={`tabular ${styles.amount}`}>{money(item)}</span>
      </div>
      <div className={styles.itemFlow}>
        <Track item={item} stages={stages} />
        <span className={styles.badge} data-tone={tone ?? (item.stage === "closed" ? "good" : "neutral")}>
          {item.stageLabel}
        </span>
      </div>
      {item.reason ? (
        <p className={styles.reason}>
          {item.reason}{" "}
          {item.href ? (
            <Link href={item.href} className={styles.inlineLink}>
              Answer in Needs you
            </Link>
          ) : null}
        </p>
      ) : null}
      <details className={styles.details}>
        <summary>
          Every step ({item.journey.length})
          <Icon name="chevronDown" size={16} />
        </summary>
        <ol className={styles.timeline}>
          {item.journey.map((j, i) => (
            <li key={`${item.id}-${i}`} data-agent={j.agent}>
              <span className={styles.tTime}>{when(j.at)}</span>
              <span>
                <strong>{j.label}</strong> <span className="meta">by {j.agentLabel}</span>
                {j.note ? <span className={styles.tNote}>{j.note}</span> : null}
                <span className="meta">
                  {j.evidence} piece{j.evidence === 1 ? "" : "s"} of evidence
                </span>
              </span>
            </li>
          ))}
        </ol>
      </details>
    </li>
  );
}

export function DiagramView({ data }: { data: Pipeline }) {
  const [filter, setFilter] = useState<Filter>({ kind: "open" });
  const { stages, side, summary } = data;
  const counts = {
    open: data.items.filter((i) => i.open).length,
    waiting: data.items.filter((i) => i.stage === "needs_owner").length,
    closed: data.items.filter((i) => i.stage === "closed").length,
    all: data.items.length,
  };
  const items = useMemo(() => {
    switch (filter.kind) {
      case "open":
        return data.items.filter((i) => i.open);
      case "waiting":
        return data.items.filter((i) => i.stage === "needs_owner");
      case "closed":
        return data.items.filter((i) => i.stage === "closed");
      case "stage":
        return data.items.filter((i) => i.stage === filter.stage.id);
      default:
        return data.items;
    }
  }, [data.items, filter]);
  const selected = filter.kind === "stage" ? filter.stage.id : null;
  const pick = (s: PipelineStage) => setFilter(selected === s.id ? { kind: "open" } : { kind: "stage", stage: s });

  return (
    <div className={styles.wrap}>
      <section className={`card ${styles.board}`} aria-labelledby="flow-h">
        <h2 id="flow-h" className="visually-hidden">
          How every item moves
        </h2>

        <div className={styles.band}>
          <p className={styles.bandLabel}>Comes in from</p>
          <ul className={styles.chips}>
            {data.sources.map((s) => (
              <li key={s.id} className={styles.source}>
                {s.label} <span className="tabular">{s.count}</span>
              </li>
            ))}
          </ul>
        </div>

        <div className={styles.down} aria-hidden="true" />

        <div className={styles.band}>
          <p className={styles.bandLabel}>Every item takes these steps, one at a time, each with evidence</p>
          <ol className={styles.track}>
            {stages.map((s, i) => {
              const state = s.id === "closed" ? "closed" : s.now > 0 ? "busy" : (s.passed ?? 0) > 0 ? "used" : "idle";
              return (
                <li key={s.id} className={styles.stage} data-state={state}>
                  <button type="button" onClick={() => pick(s)} aria-pressed={selected === s.id} title={s.description}>
                    <span className={styles.stepNo}>{i + 1}</span>
                    <span className={styles.stageLabel}>{s.label}</span>
                    <span className={`tabular ${styles.now}`}>{s.now}</span>
                    <span className={styles.nowLabel}>{s.id === "closed" ? "closed" : "here now"}</span>
                    <span className={styles.passed}>{s.passed ?? 0} passed through</span>
                  </button>
                </li>
              );
            })}
          </ol>
        </div>

        <div className={styles.aside}>
          <p className={styles.bandLabel}>Some items step aside</p>
          <ul className={styles.sideList}>
            {side.map((s) => (
              <li key={s.id} className={styles.sideBox} data-tone={SIDE_TONE[s.id]} data-empty={s.now === 0 || undefined}>
                <button type="button" onClick={() => pick(s)} aria-pressed={selected === s.id}>
                  <span className={`tabular ${styles.sideNow}`}>{s.now}</span>
                  <span>
                    <strong>{s.label}</strong>
                    <span className="meta">{s.description}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className={styles.down} aria-hidden="true" />

        <div className={styles.band}>
          <p className={styles.bandLabel}>Ends up in</p>
          <div className={styles.outputs}>
            {data.outputs.map((o) => (
              <div key={o.id} className={styles.output}>
                <p className="meta">{o.label}</p>
                <ul>
                  {o.items.map((x) => (
                    <li key={x.label} data-tone={x.tone}>
                      {x.href ? (
                        <Link href={x.href} className={styles.inlineLink}>
                          {x.label}
                        </Link>
                      ) : (
                        <span>{x.label}</span>
                      )}
                      <span className="meta">{x.detail}</span>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section aria-labelledby="items-h" className="stack-2">
        <div className={styles.sectionHead}>
          <h2 id="items-h" className="h3">
            Item by item
          </h2>
          <div className={styles.filters} role="group" aria-label="Show">
            {(
              [
                ["open", "In progress"],
                ["waiting", "Waiting for you"],
                ["closed", "Closed"],
                ["all", "All"],
              ] as const
            ).map(([kind, label]) => (
              <button key={kind} type="button" className="chip" aria-pressed={filter.kind === kind} onClick={() => setFilter({ kind })}>
                {label} <span className="tabular">{counts[kind]}</span>
              </button>
            ))}
            {filter.kind === "stage" ? (
              <button type="button" className="chip" aria-pressed onClick={() => setFilter({ kind: "open" })}>
                {filter.stage.label} <span className="tabular">{items.length}</span>
                <Icon name="x" size={14} />
                <span className="visually-hidden">Clear</span>
              </button>
            ) : null}
          </div>
        </div>
        {filter.kind === "stage" ? <p className="meta">{filter.stage.description}</p> : null}
        {items.length ? (
          <ul className={styles.items}>
            {items.map((item) => (
              <ItemRow key={item.id} item={item} stages={stages} />
            ))}
          </ul>
        ) : (
          <p className="card card-pad meta">Nothing here right now.</p>
        )}
      </section>

      <section aria-labelledby="agents-h" className="stack-2">
        <h2 id="agents-h" className="h3">
          Who did the work
        </h2>
        <p className="meta">
          {summary.steps} steps so far, each one recorded with its evidence. A fixed set of rules decides which agent works next, so no
          single AI decides everything.
        </p>
        <ul className={styles.agents}>
          {data.agents.map((a) => (
            <li key={a.id} className="card card-pad">
              <span className={`tabular ${styles.agentCount}`}>{a.count}</span>
              <span className="meta">{a.unit}</span>
              <strong>{a.label}</strong>
              <span className="meta">{a.description}</span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  );
}
