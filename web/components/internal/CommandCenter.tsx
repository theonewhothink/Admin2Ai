"use client";

/**
 * Command Center: the product's health for the team, computed by the engine
 * (GET /api/internal/overview): the spec's success targets, a health score,
 * what needs fixing now, the pipeline, tenants and companies, and go-live readiness.
 */
import Link from "next/link";
import type { CSSProperties } from "react";
import type { CriticalFix, GoldenTotal, InternalOverview, TenantRow } from "@/lib/internal-types";
import { AdminIcon, type AdminIconName } from "./icons";
import { ReadinessGrid } from "./ReadinessView";
import { useOverview } from "./store";
import { Bar, Card, Chip, COLORS, Gauge, PageHeader, Pill, Ring, scoreColor, SectionLabel, Waiting } from "./widgets";
import styles from "./admin.module.css";

const GOLDEN_LOOK: Record<string, { color: string; icon: AdminIconName }> = {
  zero_touch: { color: COLORS.amber, icon: "zap" },
  recovered: { color: COLORS.blue, icon: "inbox" },
  unresolved: { color: COLORS.purple, icon: "layers" },
  owner_minutes: { color: COLORS.green, icon: "clock" },
};

function GoldenCard({ g, index }: { g: GoldenTotal; index: number }) {
  const look = GOLDEN_LOOK[g.id] ?? { color: COLORS.slate, icon: "target" as const };
  return (
    <Card className={styles.golden} delay={index * 60} aria-label={g.label}>
      <div className={styles.goldenHead}>
        <Chip icon={look.icon} color={look.color} />
        <span className={styles.goldenHeadText}>
          <span className={styles.goldenLabel}>{g.label}</span>
          <span className={styles.goldenTarget}>Target: {g.targetLabel}</span>
        </span>
      </div>
      <div className={styles.goldenBody}>
        <Ring
          percent={g.ring}
          color={look.color}
          label={g.ringLabel}
          caption={g.ringCaption}
          ariaLabel={`${g.label}: ${g.hasData ? g.ringLabel : "no data yet"}, target ${g.targetLabel}`}
        />
        <div className={styles.goldenFigures}>
          <p className={styles.bigNumber}>
            {g.count}
            <span className={styles.bigUnit}> {g.countLabel}</span>
          </p>
          <p className={styles.remaining} data-on-target={g.onTarget === true}>
            {g.onTarget ? <AdminIcon name="checkCircle" size={14} /> : null}
            {g.remainingLabel}
          </p>
        </div>
      </div>
      <Bar percent={g.ring} color={look.color} />
      <p className={styles.detail}>
        {g.estimate ? <Pill tone="slate">Estimate</Pill> : null} {g.detail}
      </p>
    </Card>
  );
}

function HealthCard({ health }: { health: InternalOverview["health"] }) {
  return (
    <Card className={styles.health} delay={120} aria-labelledby="health-title">
      <h3 id="health-title" className={styles.cardTitle}>
        <AdminIcon name="heart" size={16} style={{ color: scoreColor(health.score) }} />
        Global health score
      </h3>
      <div className={styles.healthBody}>
        <Gauge score={health.score} />
        <ul className={styles.subScores}>
          {health.parts.map((p) => (
            <li key={p.id}>
              <div className={styles.subScoreHead}>
                <span>{p.label}</span>
                <strong style={{ color: scoreColor(p.score) }}>{p.score === null ? "–" : p.score}</strong>
              </div>
              <Bar percent={p.score ?? 0} color={scoreColor(p.score)} label={`${p.label}: ${p.score ?? "no data"} out of 100`} />
              <p className={styles.detail}>{p.detail}</p>
            </li>
          ))}
        </ul>
      </div>
    </Card>
  );
}

const FIX_ICON: Record<CriticalFix["severity"], AdminIconName> = {
  red: "alertTriangle",
  amber: "alertCircle",
  blue: "info",
};

function FixRow({ fix }: { fix: CriticalFix }) {
  const body = (
    <>
      <AdminIcon name={FIX_ICON[fix.severity]} size={18} className={styles.fixIcon} />
      <span className={styles.fixText}>
        <strong>{fix.label}</strong>
        <span>
          {fix.detail}
          {fix.company ? ` · ${fix.company}` : ""}
        </span>
      </span>
      {fix.href ? <AdminIcon name="externalLink" size={14} className={styles.fixLink} /> : null}
    </>
  );
  return (
    <li>
      {fix.href ? (
        <Link href={fix.href} prefetch={false} className={styles.fix} data-severity={fix.severity}>
          {body}
        </Link>
      ) : (
        <div className={styles.fix} data-severity={fix.severity}>
          {body}
        </div>
      )}
    </li>
  );
}

function FixesCard({ fixes }: { fixes: CriticalFix[] }) {
  return (
    <Card className={styles.fixes} delay={180} aria-labelledby="fixes-title">
      <h3 id="fixes-title" className={styles.cardTitle}>
        <AdminIcon name="alertTriangle" size={16} style={{ color: fixes.some((f) => f.severity === "red") ? COLORS.red : COLORS.green }} />
        Critical fixes
        {fixes.length ? <span className={styles.count}>{fixes.length}</span> : null}
      </h3>
      {fixes.length ? (
        <ul className={styles.fixList}>
          {fixes.map((f) => (
            <FixRow key={f.id} fix={f} />
          ))}
        </ul>
      ) : (
        <p className={styles.allGood}>
          <AdminIcon name="checkCircle" size={18} />
          All systems healthy. No critical issues found.
        </p>
      )}
    </Card>
  );
}

function PipelineCard({ pipeline }: { pipeline: InternalOverview["pipeline"] }) {
  const total = pipeline.summary.items || 0;
  const sideTone: Record<string, "amber" | "red" | "slate"> = { needs_owner: "amber", conflict: "red", not_required: "slate" };
  return (
    <Card className={styles.pipeline} delay={60} aria-labelledby="pipeline-title">
      <div className={styles.cardHeadRow}>
        <h3 id="pipeline-title" className={styles.cardTitle}>
          <AdminIcon name="workflow" size={16} style={{ color: COLORS.amber }} />
          Items per step
        </h3>
        <Link href="/diagram" prefetch={false} className={styles.textLink}>
          Open the diagram <AdminIcon name="externalLink" size={12} />
        </Link>
      </div>
      <p className={styles.muted}>
        {total} items · {pipeline.summary.steps} evidence-carrying steps. Bars show how many passed each step; the number
        on the right is how many are there now.
      </p>
      <ol className={styles.stageList}>
        {pipeline.stages.map((s) => (
          <li key={s.id}>
            <span className={styles.stageName}>{s.label}</span>
            <Bar
              percent={total ? ((s.passed ?? 0) / total) * 100 : 0}
              color={s.id === "closed" ? COLORS.green : COLORS.amber}
              label={`${s.label}: ${s.passed ?? 0} of ${total} passed, ${s.now} here now`}
              height={8}
            />
            <span className={styles.stageNums}>
              {s.passed ?? 0}
              <small> · {s.now} now</small>
            </span>
          </li>
        ))}
      </ol>
      <div className={styles.sideStates}>
        {pipeline.side.map((s) => (
          <Pill key={s.id} tone={s.now ? sideTone[s.id] ?? "slate" : "slate"}>
            {s.label}: {s.now}
          </Pill>
        ))}
      </div>
    </Card>
  );
}

function AgentsCard({ agents }: { agents: InternalOverview["pipeline"]["agents"] }) {
  const max = Math.max(1, ...agents.map((a) => a.count));
  return (
    <Card className={styles.agents} delay={120} aria-labelledby="agents-title">
      <h3 id="agents-title" className={styles.cardTitle}>
        <AdminIcon name="users" size={16} style={{ color: COLORS.purple }} />
        Agents’ work
      </h3>
      <ul className={styles.agentList}>
        {agents.map((a) => (
          <li key={a.id}>
            <span className={styles.stageName} title={a.description}>
              {a.label}
            </span>
            <Bar percent={(a.count / max) * 100} color={a.id === "owner" ? COLORS.blue : COLORS.purple} height={8} />
            <span className={styles.stageNums}>
              {a.count}
              <small> {a.unit}</small>
            </span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

const TONE_PILL: Record<string, "green" | "amber" | "red" | "slate"> = { good: "green", attention: "amber", risk: "red" };

function TenantsCard({ tenants }: { tenants: TenantRow[] }) {
  return (
    <Card className={styles.tableCard} delay={60} aria-labelledby="tenants-title">
      <h3 id="tenants-title" className={styles.visuallyHidden}>
        Tenants and companies
      </h3>
      {tenants.map((t) => (
        <div key={t.id} className={styles.tenant}>
          <div className={styles.tenantHead}>
            <Chip icon="building" color={COLORS.blue} size={16} />
            <div className={styles.tenantName}>
              <strong>{t.owner}</strong>
              <span>
                {t.email} · tenant {t.id}
              </span>
            </div>
            <div className={styles.tenantFacts}>
              <Pill tone={t.connections.healthy === t.connections.total ? "green" : "amber"}>
                {t.connections.healthy}/{t.connections.total} connections
              </Pill>
              <Pill tone={t.audit.intact ? "green" : "red"}>
                {t.audit.intact ? `Audit chain intact · ${t.audit.records}` : "Audit chain broken"}
              </Pill>
              <Pill tone={t.needsYou ? "amber" : "slate"}>{t.needsYou} waiting for owner</Pill>
            </div>
          </div>
          <table className={styles.table}>
            <thead>
              <tr>
                <th scope="col">Company</th>
                <th scope="col">{t.monthLabel}</th>
                <th scope="col" className={styles.num}>
                  Closed
                </th>
                <th scope="col" className={`${styles.num} ${styles.optional}`}>
                  Items done
                </th>
                <th scope="col" className={`${styles.num} ${styles.optional}`}>
                  Missing documents
                </th>
                <th scope="col" className={`${styles.num} ${styles.optional}`}>
                  Owner answers
                </th>
              </tr>
            </thead>
            <tbody>
              {t.companies.map((c) => (
                <tr key={c.id}>
                  <th scope="row">
                    <Link href={`/companies/${c.id}`} prefetch={false} className={styles.rowLink}>
                      {c.name}
                    </Link>
                    <small>
                      <span className={styles.legal}>{c.legalName} · </span>NIF {c.taxId}
                    </small>
                  </th>
                  <td>
                    <Pill tone={TONE_PILL[c.tone] ?? "slate"}>{c.statusLabel}</Pill>
                  </td>
                  <td className={styles.num}>
                    <span className={styles.closedCell}>
                      {c.percentClosed}%
                      <Bar percent={c.percentClosed} color={c.percentClosed === 100 ? COLORS.green : COLORS.amber} height={4} />
                    </span>
                  </td>
                  <td className={`${styles.num} ${styles.optional}`}>
                    {c.itemsDone} / {c.itemsTotal}
                  </td>
                  <td className={`${styles.num} ${styles.optional}`}>{c.missingDocuments}</td>
                  <td className={`${styles.num} ${styles.optional}`}>{c.needsYou}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </Card>
  );
}

const QUICK_COLORS = [COLORS.amber, COLORS.blue, COLORS.purple, COLORS.green];

function QuickStats({ quick }: { quick: InternalOverview["quick"] }) {
  return (
    <Card className={styles.quick} delay={60} aria-label="Quick stats">
      <dl>
        {quick.map((q, i) => (
          <div key={q.id}>
            <dt>{q.label}</dt>
            <dd style={{ color: QUICK_COLORS[i % QUICK_COLORS.length] } as CSSProperties}>{q.value.toLocaleString("en-GB")}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

export function CommandCenter() {
  const { data, loading, loaded, refresh } = useOverview();
  return (
    <div className={styles.page}>
      <PageHeader title="Command Center" subtitle="Real-time platform overview" loading={loading} onRefresh={() => void refresh()} />
      {data ? <Overview data={data} /> : <Waiting loaded={loaded && !loading} what="the Command Center" />}
    </div>
  );
}

function Overview({ data }: { data: InternalOverview }) {
  const r = data.readiness;
  return (
    <>
      <section aria-labelledby="golden-label" className={styles.section}>
        <SectionLabel id="golden-label">Golden totals · {data.period.label}</SectionLabel>
        <div className={styles.goldenGrid}>
          {data.golden.map((g, i) => (
            <GoldenCard key={g.id} g={g} index={i} />
          ))}
        </div>
      </section>

      <section aria-labelledby="health-label" className={styles.section}>
        <SectionLabel id="health-label">Health and fixes</SectionLabel>
        <div className={styles.healthGrid}>
          <HealthCard health={data.health} />
          <FixesCard fixes={data.fixes} />
        </div>
      </section>

      <section aria-labelledby="pipeline-label" className={styles.section}>
        <SectionLabel id="pipeline-label">Pipeline throughput</SectionLabel>
        <div className={styles.twoGrid}>
          <PipelineCard pipeline={data.pipeline} />
          <AgentsCard agents={data.pipeline.agents} />
        </div>
      </section>

      <section aria-labelledby="tenants-label" className={styles.section}>
        <SectionLabel id="tenants-label">Tenants and companies</SectionLabel>
        <TenantsCard tenants={data.tenants} />
      </section>

      <section aria-labelledby="readiness-label" className={styles.section}>
        <div className={styles.sectionHeadRow}>
          <SectionLabel id="readiness-label">
            Readiness · {r.live} of {r.total} live · {r.percent}%
          </SectionLabel>
          <Link href="/internal/readiness" className={styles.textLink}>
            Full checklist
          </Link>
        </div>
        <ReadinessGrid items={r.items} compact />
      </section>

      <section aria-label="Quick stats" className={styles.section}>
        <QuickStats quick={data.quick} />
      </section>
    </>
  );
}
