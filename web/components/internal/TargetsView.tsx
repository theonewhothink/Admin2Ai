"use client";

/** Targets: every success target in the spec (§59), with the engine's figure and how it is worked out. */
import type { TargetRow } from "@/lib/internal-types";
import { AdminIcon } from "./icons";
import { useOverview } from "./store";
import { Card, PageHeader, Pill, SectionLabel, Waiting } from "./widgets";
import styles from "./admin.module.css";

function Status({ t }: { t: TargetRow }) {
  if (t.onTarget === null) return <Pill tone="slate">No data yet</Pill>;
  return t.onTarget ? <Pill tone="green">On target</Pill> : <Pill tone="amber">Below target</Pill>;
}

export function TargetsView() {
  const { data, loading, loaded, refresh } = useOverview();
  return (
    <div className={styles.page}>
      <PageHeader
        title="Targets"
        subtitle="The spec’s success targets, measured by the engine"
        loading={loading}
        onRefresh={() => void refresh()}
      />
      {data ? (
        <section className={styles.section} aria-labelledby="targets-label">
          <SectionLabel id="targets-label">
            {data.period.label} · {data.targets.filter((t) => t.onTarget).length} of{" "}
            {data.targets.filter((t) => t.onTarget !== null).length} measured targets met
          </SectionLabel>
          <ul className={styles.targetList}>
            {data.targets.map((t, i) => (
              <Card key={t.id} as="li" className={styles.target} delay={i * 40} aria-label={t.label}>
                <div className={styles.targetMain}>
                  <p className={styles.targetLabel}>
                    {t.label}
                    {t.estimate ? <Pill tone="slate">Estimate</Pill> : null}
                  </p>
                  <p className={styles.detail}>{t.definition}</p>
                </div>
                <div className={styles.targetValue}>
                  <strong>{t.display}</strong>
                  <span>{t.evidence}</span>
                </div>
                <div className={styles.targetGoal}>
                  <span>
                    <AdminIcon name="target" size={14} /> {t.target}
                  </span>
                  <Status t={t} />
                </div>
              </Card>
            ))}
          </ul>
        </section>
      ) : (
        <Waiting loaded={loaded && !loading} what="the targets" />
      )}
    </div>
  );
}
