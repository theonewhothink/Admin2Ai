"use client";

/** Connections: every connector of every tenant, with its last sync and what it covers (§47). */
import type { InternalConnection } from "@/lib/internal-types";
import type { AdminIconName } from "./icons";
import { useOverview } from "./store";
import { ago, Card, Chip, COLORS, PageHeader, Pill, SectionLabel, Waiting, when } from "./widgets";
import styles from "./admin.module.css";

const KIND_ICON: Record<string, AdminIconName> = { email: "mail", bank: "bank", accountant: "users" };
const KIND_LABEL: Record<string, string> = { email: "Email", bank: "Bank", accountant: "Accountant" };

function day(iso: string | null) {
  return iso ? when(iso).split(",")[0] : "–";
}

function ConnectionCard({ c, now, index }: { c: InternalConnection; now: string; index: number }) {
  const ok = c.status === "healthy";
  return (
    <Card as="li" className={styles.connection} delay={index * 50} aria-label={`${c.name}, ${ok ? "syncing" : "not syncing"}`}>
      <div className={styles.readinessHead}>
        <Chip icon={KIND_ICON[c.kind] ?? "plug"} color={ok ? COLORS.blue : COLORS.red} size={16} />
        <div className={styles.connName}>
          <strong>{c.name}</strong>
          <span>{c.account}</span>
        </div>
        <Pill tone={ok ? "green" : "red"}>{ok ? "Syncing" : "Not syncing"}</Pill>
      </div>
      {c.message ? <p className={styles.connProblem}>{c.message}</p> : null}
      <dl className={styles.connFacts}>
        <div>
          <dt>Kind</dt>
          <dd>{KIND_LABEL[c.kind] ?? c.kind}</dd>
        </div>
        <div>
          <dt>Last sync</dt>
          <dd>
            {ago(c.lastSyncedAt, now)} <small>({when(c.lastSyncedAt)})</small>
          </dd>
        </div>
        <div>
          <dt>Covers</dt>
          <dd>
            {day(c.coveredFrom)} to {day(c.coveredUntil)}
          </dd>
        </div>
        <div>
          <dt>Companies</dt>
          <dd>{c.companies.join(", ") || "–"}</dd>
        </div>
      </dl>
      <p className={styles.detail}>
        {c.signIn} Tenant {c.tenant}.
      </p>
    </Card>
  );
}

export function ConnectionsView() {
  const { data, loading, loaded, refresh } = useOverview();
  const conns = data?.connections ?? [];
  const ok = conns.filter((c) => c.status === "healthy").length;
  return (
    <div className={styles.page}>
      <PageHeader
        title="Connections"
        subtitle="Every email, bank and accountant connection, and when it last synced"
        loading={loading}
        onRefresh={() => void refresh()}
      />
      {data ? (
        <section className={styles.section} aria-labelledby="conn-label">
          <SectionLabel id="conn-label">
            {ok} of {conns.length} syncing · as of {when(data.generatedAt)}
          </SectionLabel>
          <ul className={styles.readinessGrid}>
            {conns.map((c, i) => (
              <ConnectionCard key={c.id} c={c} now={data.generatedAt} index={i} />
            ))}
          </ul>
          <p className={`${styles.muted} ${styles.note}`}>
            A connection that stops syncing keeps every month it covers from closing until it is back (§47).
          </p>
        </section>
      ) : (
        <Waiting loaded={loaded && !loading} what="connections" />
      )}
    </div>
  );
}
