"use client";

/**
 * QA: the 50 real-world SME case studies and the universal client acceptance
 * checklist, each with an honest verdict. The data and the verdicts live in
 * backend/src/backoffice/acceptance.py (GET /api/internal/acceptance); every
 * "pass" there cites an automated test that proves it, and
 * backend/tests/test_acceptance.py keeps that true. This page only shows it.
 */
import { useMemo, useState, type CSSProperties } from "react";
import type {
  AcceptanceCase,
  AcceptanceCheck,
  AcceptanceCounts,
  AcceptanceStatus,
  InternalAcceptance,
} from "@/lib/internal-types";
import { useAcceptance } from "./store";
import { Bar, Card, COLORS, PageHeader, Pill, Ring, SectionLabel, Waiting } from "./widgets";
import styles from "./admin.module.css";

type Tab = "cases" | "checklist" | "sector" | "final";
type Filter = "all" | AcceptanceStatus;

const TONE: Record<AcceptanceStatus, "green" | "amber" | "red"> = { pass: "green", partial: "amber", missing: "red" };
const WORD: Record<AcceptanceStatus, string> = { pass: "Pass", partial: "Partly", missing: "Missing" };
const PASS_TEST: Record<AcceptanceStatus, string> = {
  pass: "PASS test met",
  partial: "PASS test partly met",
  missing: "PASS test not met",
};
const COLOR: Record<AcceptanceStatus, string> = { pass: COLORS.green, partial: COLORS.amber, missing: COLORS.red };

function StatusPill({ status }: { status: AcceptanceStatus }) {
  return <Pill tone={TONE[status]}>{WORD[status]}</Pill>;
}

function Dot({ status }: { status: AcceptanceStatus }) {
  return (
    <span className={styles.qaDot} style={{ background: COLOR[status] } as CSSProperties} aria-label={WORD[status]} />
  );
}

function CountsLine({ counts }: { counts: AcceptanceCounts }) {
  return (
    <span className={styles.qaCounts}>
      <span data-tone="green">{counts.pass} pass</span>
      <span data-tone="amber">{counts.partial} partly</span>
      <span data-tone="red">{counts.missing} missing</span>
    </span>
  );
}

function Evidence({ refs }: { refs: string[] }) {
  if (!refs.length) return null;
  return (
    <details className={styles.qaProof}>
      <summary>
        Proof · {refs.length} {refs.length === 1 ? "test" : "tests"}
      </summary>
      <ul>
        {refs.map((r) => (
          <li key={r} className={`${styles.mono} ${styles.breakAll}`}>
            {r}
          </li>
        ))}
      </ul>
    </details>
  );
}

function CheckRow({ check }: { check: AcceptanceCheck }) {
  return (
    <li className={styles.qaCheck} data-status={check.status}>
      <div className={styles.qaCheckHead}>
        <span className={`${styles.mono} ${styles.qaId}`}>{check.id}</span>
        <span className={styles.qaCheckText}>{check.text}</span>
        <StatusPill status={check.status} />
      </div>
      <p className={styles.qaWhere}>
        <span>{check.where}</span>
        {check.note ? <span className={styles.muted}> · {check.note}</span> : null}
      </p>
      <Evidence refs={check.evidence} />
    </li>
  );
}

function CaseCard({ item, index }: { item: AcceptanceCase; index: number }) {
  const pct = item.handled ? Math.round((item.covered / item.handled) * 100) : 0;
  return (
    <Card
      as="li"
      className={styles.qaCase}
      delay={Math.min(index, 10) * 30}
      style={{ "--edge": `${COLOR[item.verdict]}55` } as CSSProperties}
      aria-label={`Case ${item.number}, ${item.title}: ${WORD[item.verdict]}`}
    >
      <div className={styles.qaCaseHead}>
        <span className={`${styles.mono} ${styles.qaId}`}>{String(item.number).padStart(2, "0")}</span>
        <strong className={styles.qaCaseTitle}>{item.title}</strong>
        <StatusPill status={item.verdict} />
      </div>
      <p className={styles.qaPassLine}>
        <Dot status={item.passTest.status} /> {PASS_TEST[item.passTest.status]}
      </p>
      <div className={styles.readinessProgress}>
        <Bar percent={pct} color={COLOR[item.verdict]} label={`${item.covered} of ${item.handled} handled`} />
        <span>
          {item.covered}/{item.handled}
        </span>
      </div>
      <details className={styles.qaMore}>
        <summary>Details</summary>
        <blockquote className={styles.qaQuote}>&ldquo;{item.quote}&rdquo;</blockquote>
        <p className={styles.qaSub}>System must handle</p>
        <ul className={styles.qaHandles}>
          {item.handles.map((h) => (
            <li key={`${h.text}-${h.check}`}>
              <Dot status={h.status} />
              <span>
                {h.text} <span className={`${styles.mono} ${styles.muted}`}>{h.check}</span>
                {h.status !== "pass" && h.note ? <small className={styles.muted}> — {h.note}</small> : null}
              </span>
            </li>
          ))}
        </ul>
        <p className={styles.qaSub}>PASS</p>
        <p className={styles.detail}>{item.passTest.text}</p>
        <p className={styles.qaDepends}>
          Depends on{" "}
          {item.passTest.checks.map((c, i) => (
            <span key={c.id}>
              {i ? ", " : ""}
              <Dot status={c.status} /> <span className={styles.mono}>{c.id}</span>
            </span>
          ))}
        </p>
        {item.gaps.length ? (
          <>
            <p className={styles.qaSub}>Still open</p>
            <ul className={styles.qaHandles}>
              {item.gaps.map((g) => (
                <li key={g.id}>
                  <Dot status={g.status} />
                  <span>
                    <span className={styles.mono}>{g.id}</span> {g.text}
                    {g.note ? <small className={styles.muted}> — {g.note}</small> : null}
                  </span>
                </li>
              ))}
            </ul>
          </>
        ) : null}
      </details>
    </Card>
  );
}

function FilterBar({
  value,
  onChange,
  counts,
  label,
}: {
  value: Filter;
  onChange: (f: Filter) => void;
  counts: AcceptanceCounts;
  label: string;
}) {
  const options: { id: Filter; label: string; n: number }[] = [
    { id: "all", label: "All", n: counts.total },
    { id: "pass", label: "Pass", n: counts.pass },
    { id: "partial", label: "Partly", n: counts.partial },
    { id: "missing", label: "Missing", n: counts.missing },
  ];
  return (
    <div className={styles.filters} role="group" aria-label={label}>
      {options.map((o) => (
        <button
          key={o.id}
          type="button"
          className={styles.filter}
          aria-pressed={value === o.id}
          onClick={() => onChange(o.id)}
        >
          {o.label} <small>{o.n}</small>
        </button>
      ))}
    </div>
  );
}

function matches(text: string, query: string): boolean {
  return !query || text.toLowerCase().includes(query.toLowerCase());
}

function CasesTab({ data }: { data: InternalAcceptance }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const shown = data.cases.filter(
    (c) =>
      (filter === "all" || c.verdict === filter) &&
      matches(`${c.number} ${c.title} ${c.quote} ${c.handles.map((h) => h.text).join(" ")}`, query),
  );
  return (
    <section className={styles.section} aria-labelledby="qa-cases-label">
      <SectionLabel id="qa-cases-label">Case studies · {data.cases.length}</SectionLabel>
      <div className={styles.qaToolbar}>
        <FilterBar value={filter} onChange={setFilter} counts={data.summary.cases} label="Filter cases" />
        <input
          type="search"
          className={styles.qaSearch}
          placeholder="Search cases"
          aria-label="Search cases"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      {shown.length ? (
        <ul className={styles.qaGrid}>
          {shown.map((c, i) => (
            <CaseCard key={c.number} item={c} index={i} />
          ))}
        </ul>
      ) : (
        <p className={styles.muted}>No case matches.</p>
      )}
    </section>
  );
}

function ChecklistTab({ data }: { data: InternalAcceptance }) {
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const sections = data.sections
    .map((s) => ({
      ...s,
      shown: s.checks.filter(
        (c) => (filter === "all" || c.status === filter) && matches(`${c.id} ${c.text} ${c.note} ${c.where}`, query),
      ),
    }))
    .filter((s) => s.shown.length);
  const filtering = filter !== "all" || query !== "";
  return (
    <section className={styles.section} aria-labelledby="qa-checklist-label">
      <SectionLabel id="qa-checklist-label">Universal client acceptance checklist · {data.summary.checklist.total}</SectionLabel>
      <div className={styles.qaToolbar}>
        <FilterBar value={filter} onChange={setFilter} counts={data.summary.checklist} label="Filter checks" />
        <input
          type="search"
          className={styles.qaSearch}
          placeholder="Search checks"
          aria-label="Search checks"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </div>
      <ul className={styles.qaSections}>
        {sections.map((s, i) => {
          const pct = s.counts.total ? Math.round((s.counts.pass / s.counts.total) * 100) : 0;
          return (
            <Card as="li" key={s.id} className={styles.qaSection} delay={Math.min(i, 10) * 25}>
              <details open={filtering || undefined}>
                <summary className={styles.qaSectionHead}>
                  <span className={`${styles.mono} ${styles.qaLetter}`}>{s.id}</span>
                  <strong>{s.title}</strong>
                  <CountsLine counts={s.counts} />
                  <span className={styles.qaSectionBar}>
                    <Bar percent={pct} color={pct === 100 ? COLORS.green : COLORS.amber} />
                  </span>
                </summary>
                <ul className={styles.qaChecks}>
                  {s.shown.map((c) => (
                    <CheckRow key={c.id} check={c} />
                  ))}
                </ul>
              </details>
            </Card>
          );
        })}
      </ul>
      {!sections.length ? <p className={styles.muted}>No check matches.</p> : null}
    </section>
  );
}

function SectorTab({ data }: { data: InternalAcceptance }) {
  const [filter, setFilter] = useState<Filter>("all");
  const shown = data.sector.filter((c) => filter === "all" || c.status === filter);
  return (
    <section className={styles.section} aria-labelledby="qa-sector-label">
      <SectionLabel id="qa-sector-label">Sector capabilities the cases need · {data.sector.length}</SectionLabel>
      <p className={styles.muted}>
        Things the 50 cases require that the checklist does not name, such as payouts, cost centers, deposits and cash.
      </p>
      <FilterBar value={filter} onChange={setFilter} counts={data.summary.sector} label="Filter sector capabilities" />
      <Card className={styles.qaSection}>
        <ul className={styles.qaChecks}>
          {shown.map((c) => (
            <CheckRow key={c.id} check={c} />
          ))}
        </ul>
      </Card>
    </section>
  );
}

function FinalTab({ data }: { data: InternalAcceptance }) {
  const f = data.finalTest;
  return (
    <section className={styles.section} aria-labelledby="qa-final-label">
      <SectionLabel id="qa-final-label">Final test · after 30 days</SectionLabel>
      <Card className={styles.qaFinal}>
        <div className={styles.qaCaseHead}>
          <strong className={styles.qaCaseTitle}>Ask every customer</strong>
          <StatusPill status={f.status} />
        </div>
        <blockquote className={styles.qaQuote}>&ldquo;{f.question}&rdquo;</blockquote>
        <p className={styles.qaSub}>Every answer must become one of</p>
        <ol className={styles.qaFinalList}>
          {f.categories.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ol>
        <p className={styles.detail}>{f.rule}</p>
        <p className={styles.muted}>{f.note}</p>
      </Card>
    </section>
  );
}

export function QAView() {
  const { data, loading, loaded, refresh } = useAcceptance();
  const [tab, setTab] = useState<Tab>("cases");
  const s = data?.summary;
  const tabs = useMemo(
    () =>
      s
        ? ([
            { id: "cases", label: "Case studies", n: `${s.cases.pass}/${s.cases.total}` },
            { id: "checklist", label: "Checklist", n: `${s.checklist.pass}/${s.checklist.total}` },
            { id: "sector", label: "Sector", n: `${s.sector.pass}/${s.sector.total}` },
            { id: "final", label: "Final test", n: "" },
          ] as { id: Tab; label: string; n: string }[])
        : [],
    [s],
  );
  return (
    <div className={styles.page}>
      <PageHeader
        title="QA"
        subtitle="50 real-world SME cases and the client acceptance checklist, checked against the engine"
        loading={loading}
        onRefresh={() => void refresh()}
      />
      {data && s ? (
        <>
          <div className={styles.qaSummary}>
            <Card className={styles.readinessSummary} aria-label="Checklist proven">
              <Ring
                percent={s.percent}
                color={s.percent >= 95 ? COLORS.green : COLORS.amber}
                label={`${s.percent}%`}
                caption="proven"
                ariaLabel={`${s.percent}% of the checklist proven`}
              />
              <div>
                <p className={styles.bigNumber}>
                  {s.checklist.pass}
                  <span className={styles.bigUnit}> of {s.checklist.total} checks</span>
                </p>
                <CountsLine counts={s.checklist} />
              </div>
            </Card>
            <Card className={styles.qaStat} delay={40} aria-label="Cases">
              <p className={styles.qaStatLabel}>Cases that fully pass</p>
              <p className={styles.bigNumber}>
                {s.cases.pass}
                <span className={styles.bigUnit}> of {s.cases.total}</span>
              </p>
              <Bar percent={(s.cases.pass / s.cases.total) * 100} color={COLORS.green} />
              <p className={styles.muted}>
                {s.passTests.pass} of {s.passTests.total} PASS tests met; the rest wait on something each case must
                handle.
              </p>
            </Card>
            <Card className={styles.qaStat} delay={80} aria-label="Sector capabilities">
              <p className={styles.qaStatLabel}>Sector capabilities</p>
              <p className={styles.bigNumber}>
                {s.sector.pass}
                <span className={styles.bigUnit}> of {s.sector.total}</span>
              </p>
              <Bar percent={(s.sector.pass / s.sector.total) * 100} color={COLORS.green} />
              <p className={styles.muted}>Payouts, cost centers, deposits, cash, leasing, imports and more.</p>
            </Card>
          </div>
          <Card className={styles.qaLegend} delay={100}>
            <p className={styles.detail}>{data.purpose}</p>
            <ul>
              {(["pass", "partial", "missing"] as AcceptanceStatus[]).map((st) => (
                <li key={st}>
                  <StatusPill status={st} /> <span className={styles.muted}>{data.legend[st]}</span>
                </li>
              ))}
            </ul>
          </Card>
          <div className={`${styles.filters} ${styles.qaTabs}`} role="tablist" aria-label="QA views">
            {tabs.map((t) => (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={tab === t.id}
                className={styles.filter}
                onClick={() => setTab(t.id)}
              >
                {t.label} {t.n ? <small>{t.n}</small> : null}
              </button>
            ))}
          </div>
          {tab === "cases" ? <CasesTab data={data} /> : null}
          {tab === "checklist" ? <ChecklistTab data={data} /> : null}
          {tab === "sector" ? <SectorTab data={data} /> : null}
          {tab === "final" ? <FinalTab data={data} /> : null}
        </>
      ) : (
        <Waiting loaded={loaded && !loading} what="the QA checklist" />
      )}
    </div>
  );
}
