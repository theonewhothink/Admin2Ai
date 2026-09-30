"use client";

/**
 * Production readiness: the owner's go-live tracker. The list itself is data
 * in backend/src/backoffice/readiness.py; this page only shows it.
 */
import type { CSSProperties } from "react";
import type { ReadinessItem } from "@/lib/internal-types";
import type { AdminIconName } from "./icons";
import { useOverview } from "./store";
import { Bar, Card, Chip, COLORS, PageHeader, Pill, Ring, SectionLabel, Waiting } from "./widgets";
import styles from "./admin.module.css";

const AREA_ICON: Record<string, AdminIconName> = {
  site: "globe",
  operations: "branch",
  accounts: "userCheck",
  data: "database",
  connections: "link",
  documents: "scan",
  mobile: "phone",
  trust: "shield",
  billing: "card",
};

const ICON_BY_ID: Record<string, AdminIconName> = {
  email: "mail",
  bank: "bank",
  monitoring: "gauge",
};

function icon(item: ReadinessItem): AdminIconName {
  return ICON_BY_ID[item.id] ?? AREA_ICON[item.area] ?? "listChecks";
}

export function ReadinessGrid({ items, compact = false }: { items: ReadinessItem[]; compact?: boolean }) {
  return (
    <ul className={styles.readinessGrid}>
      {items.map((item, i) => {
        const live = item.status === "live";
        const color = live ? COLORS.green : COLORS.amber;
        return (
          <Card
            key={item.id}
            as="li"
            className={styles.readiness}
            delay={Math.min(i, 8) * 40}
            style={{ "--edge": live ? "rgba(34, 197, 94, 0.3)" : "rgba(245, 158, 11, 0.2)" } as CSSProperties}
            aria-label={`${item.title}: ${live ? "live" : `pending, ${item.percent}% done`}`}
          >
            <div className={styles.readinessHead}>
              <Chip icon={icon(item)} color={color} size={16} />
              <strong className={styles.readinessTitle}>{item.title}</strong>
              <Pill tone={live ? "green" : "amber"}>{live ? "Live" : "Pending"}</Pill>
            </div>
            <div className={styles.readinessProgress}>
              <Bar percent={item.percent} color={color} />
              <span>{item.percent}%</span>
            </div>
            <p className={styles.detail} data-compact={compact || undefined}>
              {item.detail}
            </p>
          </Card>
        );
      })}
    </ul>
  );
}

export function ReadinessView() {
  const { data, loading, loaded, refresh } = useOverview();
  const r = data?.readiness;
  return (
    <div className={styles.page}>
      <PageHeader
        title="Readiness"
        subtitle="What is live and what remains before real customers"
        loading={loading}
        onRefresh={() => void refresh()}
      />
      {r ? (
        <>
          <Card className={styles.readinessSummary} aria-label="Go-live progress">
            <Ring
              percent={r.percent}
              color={COLORS.amber}
              label={`${r.percent}%`}
              caption="overall"
              ariaLabel={`Overall ${r.percent}% ready`}
            />
            <div>
              <p className={styles.bigNumber}>
                {r.live}
                <span className={styles.bigUnit}> of {r.total} live</span>
              </p>
              <p className={styles.muted}>
                {r.pending} pending. Each item counts the same; the overall figure is rounded down.
              </p>
            </div>
          </Card>
          <section className={styles.section} aria-labelledby="pending-label">
            <SectionLabel id="pending-label">Pending · {r.pending}</SectionLabel>
            <ReadinessGrid items={r.items.filter((i) => i.status === "pending")} />
          </section>
          <section className={styles.section} aria-labelledby="live-label">
            <SectionLabel id="live-label">Live · {r.live}</SectionLabel>
            <ReadinessGrid items={r.items.filter((i) => i.status === "live")} />
          </section>
        </>
      ) : (
        <Waiting loaded={loaded && !loading} what="the readiness checklist" />
      )}
    </div>
  );
}
