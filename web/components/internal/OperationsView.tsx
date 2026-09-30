"use client";

/**
 * Operations: recent activity across tenants and the audit trail the engine
 * keeps (GET /api/internal/operations). The audit chain is verified on every
 * load; a broken chain shows in red.
 */
import { useState } from "react";
import type { InternalOperations } from "@/lib/internal-types";
import { AdminIcon, type AdminIconName } from "./icons";
import { refreshOperations, useOperations } from "./store";
import { Card, Chip, COLORS, PageHeader, Pill, SectionLabel, Waiting, when } from "./widgets";
import styles from "./admin.module.css";

const KIND: Record<string, { icon: AdminIconName; color: string }> = {
  collected: { icon: "inbox", color: COLORS.blue },
  recovered: { icon: "inbox", color: COLORS.green },
  chased: { icon: "mail", color: COLORS.amber },
  answered: { icon: "checkCircle", color: COLORS.green },
  checked: { icon: "checkCircle", color: COLORS.blue },
  closed: { icon: "checkCircle", color: COLORS.green },
  protected: { icon: "shield", color: COLORS.red },
  learned: { icon: "zap", color: COLORS.purple },
};

function money(amount: number | undefined, currency: string | undefined) {
  if (amount === undefined) return null;
  return new Intl.NumberFormat("en-IE", { style: "currency", currency: currency || "EUR" }).format(amount);
}

function Activity({ items }: { items: InternalOperations["activity"] }) {
  return (
    <Card className={styles.activityCard} aria-labelledby="activity-title">
      <h3 id="activity-title" className={styles.cardTitle}>
        <AdminIcon name="activity" size={16} style={{ color: COLORS.amber }} />
        Activity feed
        <span className={styles.count}>{items.length}</span>
      </h3>
      <ol className={styles.feed}>
        {items.map((a) => {
          const look = KIND[a.kind] ?? { icon: "info" as const, color: COLORS.slate };
          const amount = money(a.amount, a.currency);
          return (
            <li key={a.id}>
              <Chip icon={look.icon} color={look.color} size={14} />
              <div>
                <p className={styles.feedText}>{a.text}</p>
                <p className={styles.detail}>
                  {[when(a.at), a.company, amount, a.kind].filter(Boolean).join(" · ")}
                </p>
              </div>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

function Audit({ audit }: { audit: InternalOperations["audit"] }) {
  const [agent, setAgent] = useState<string | null>(null);
  const [expanding, setExpanding] = useState(false);
  const rows = agent ? audit.entries.filter((e) => e.agent === agent) : audit.entries;
  const more = audit.records > audit.shown;
  return (
    <Card className={styles.auditCard} aria-labelledby="audit-title">
      <div className={styles.cardHeadRow}>
        <h3 id="audit-title" className={styles.cardTitle}>
          <AdminIcon name="shield" size={16} style={{ color: audit.intact ? COLORS.green : COLORS.red }} />
          Sealed records
          <span className={styles.count}>
            {audit.shown} of {audit.records}
          </span>
        </h3>
        {more ? (
          <button
            type="button"
            className={styles.ghostButton}
            disabled={expanding}
            onClick={() => {
              setExpanding(true);
              void refreshOperations(1000).finally(() => setExpanding(false));
            }}
          >
            {expanding ? "Loading" : `Show all ${audit.records}`}
          </button>
        ) : null}
      </div>
      <div className={styles.filters} role="group" aria-label="Filter by agent">
        <button type="button" className={styles.filter} aria-pressed={agent === null} onClick={() => setAgent(null)}>
          All
        </button>
        {audit.agents.map((a) => (
          <button
            key={a.id}
            type="button"
            className={styles.filter}
            aria-pressed={agent === a.id}
            onClick={() => setAgent(agent === a.id ? null : a.id)}
          >
            {a.id.replace(/_/g, " ")} <small>{a.count}</small>
          </button>
        ))}
      </div>
      {rows.length ? (
        <div className={styles.tableScroll}>
          <table className={`${styles.table} ${styles.auditTable}`}>
            <thead>
              <tr>
                <th scope="col" className={`${styles.num} ${styles.colSeq}`}>
                  #
                </th>
                <th scope="col" className={styles.colTime}>
                  Time
                </th>
                <th scope="col" className={styles.colAgent}>
                  Agent · action
                </th>
                <th scope="col">What was recorded</th>
                <th scope="col" className={`${styles.num} ${styles.optional} ${styles.colEvidence}`}>
                  Evidence
                </th>
                <th scope="col" className={`${styles.optional} ${styles.colHash}`}>
                  Hash
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((e) => (
                <tr key={e.id}>
                  <td className={`${styles.num} ${styles.mono} ${styles.cellSeq}`}>{e.seq}</td>
                  <td className={`${styles.nowrap} ${styles.cellTime}`}>{when(e.at)}</td>
                  <td className={styles.cellAgent}>
                    <strong className={styles.agentName}>{e.agent.replace(/_/g, " ")}</strong>
                    <small>{e.action.replace(/_/g, " ")}</small>
                  </td>
                  <td className={styles.cellWhat}>
                    <span className={styles.summary}>{e.summary || "–"}</span>
                    {e.subject ? <small className={styles.mono}>{e.subject}</small> : null}
                  </td>
                  <td className={`${styles.num} ${styles.optional}`}>{e.evidence}</td>
                  <td className={`${styles.mono} ${styles.optional}`}>{e.hash}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className={styles.muted}>No records from this agent among the ones shown.</p>
      )}
    </Card>
  );
}

function Chains({ audit }: { audit: InternalOperations["audit"] }) {
  return (
    <div className={styles.chainRow}>
      {audit.chains.map((c) => (
        <Card key={c.tenant} className={styles.chain} aria-label={`Audit chain for ${c.tenant}`}>
          <Chip icon={c.intact ? "shield" : "alertTriangle"} color={c.intact ? COLORS.green : COLORS.red} />
          <div>
            <p className={styles.chainTitle}>
              {c.intact ? "Audit chain intact" : "Audit chain broken"} <Pill tone={c.intact ? "green" : "red"}>{c.tenant}</Pill>
            </p>
            <p className={styles.detail}>
              {c.records} records verified, each linked to the one before by its hash.
              {c.intact ? "" : ` Problem: ${c.problem?.replace(/_/g, " ")}. ${c.detail}`}
            </p>
            <p className={`${styles.detail} ${styles.mono} ${styles.breakAll}`}>Head {c.head}</p>
          </div>
        </Card>
      ))}
    </div>
  );
}

export function OperationsView() {
  const { data, loading, loaded } = useOperations();
  return (
    <div className={styles.page}>
      <PageHeader
        title="Operations"
        subtitle="Recent activity and the audit trail, from the engine’s own records"
        loading={loading}
        onRefresh={() => void refreshOperations()}
      />
      {data ? (
        <>
          <section className={styles.section} aria-labelledby="chain-label">
            <SectionLabel id="chain-label">Audit chain</SectionLabel>
            <Chains audit={data.audit} />
          </section>
          <section className={styles.section} aria-labelledby="trail-label">
            <SectionLabel id="trail-label">Audit trail · newest first</SectionLabel>
            <Audit audit={data.audit} />
          </section>
          <section className={styles.section} aria-labelledby="activity-label">
            <SectionLabel id="activity-label">Recent activity · every tenant</SectionLabel>
            <Activity items={data.activity} />
          </section>
        </>
      ) : (
        <Waiting loaded={loaded && !loading} what="operations" />
      )}
    </div>
  );
}
