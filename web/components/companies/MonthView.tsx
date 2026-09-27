import Link from "next/link";
import { Icon } from "@/components/Icon";
import { CheckList, Disclosure, Dot, Progress } from "@/components/ui";
import { formatDay, formatDayShort, formatMoney, formatMonth, formatNumber, plural } from "@/lib/format";
import type { MonthClose, MonthStats } from "@/lib/types";
import styles from "./companies.module.css";

function statRows(s: MonthStats): { value: number; label: string; good?: boolean }[] {
  return [
    { value: s.transactionsChecked, label: `${plural(s.transactionsChecked, "transaction", "transactions")} checked` },
    { value: s.documentsCollected, label: `${plural(s.documentsCollected, "document", "documents")} collected` },
    {
      value: s.missingDocumentsRetrieved,
      label: `missing ${plural(s.missingDocumentsRetrieved, "document", "documents")} retrieved automatically`,
    },
    { value: s.suppliersChased, label: `${plural(s.suppliersChased, "supplier", "suppliers")} chased` },
    {
      value: s.accountantQuestionsResolved,
      label: `accountant ${plural(s.accountantQuestionsResolved, "question", "questions")} resolved`,
    },
    { value: s.taxObligationsVerified, label: `tax ${plural(s.taxObligationsVerified, "obligation", "obligations")} verified` },
    { value: s.unresolvedIssues, label: `unresolved ${plural(s.unresolvedIssues, "issue", "issues")}`, good: s.unresolvedIssues === 0 },
  ];
}

export function MonthView({ month }: { month: MonthClose }) {
  const name = formatMonth(month.month);
  const closed = month.status === "closed";
  const stats = statRows(month.stats);
  const matchedCount = month.stats.transactionsChecked;

  return (
    <div className="stack-4">
      {month.notices?.map((n) => (
        <div key={n.id} className={`notice notice-${n.tone === "risk" ? "risk" : "attention"} ${styles.notice}`}>
          <Dot tone={n.tone} />
          <p className={styles.noticeText}>{n.text}</p>
          {n.href ? (
            <Link href={n.href} className="btn btn-secondary">
              {n.linkLabel ?? "Open"}
            </Link>
          ) : null}
        </div>
      ))}

      <section className={`card ${styles.monthCard}`} aria-labelledby="month-h">
        {closed ? (
          <div className={styles.monthHead}>
            <span className={styles.closedIcon}>
              <Icon name="check" size={24} strokeWidth={2.2} />
            </span>
            <div className="stack-1">
              <h2 id="month-h" className={styles.monthTitle}>
                {name} is closed.
              </h2>
              {month.closedOn ? <p className="muted">Closed on {formatDay(month.closedOn)}.</p> : null}
            </div>
          </div>
        ) : (
          <div className="stack-2">
            <h2 id="month-h" className={styles.monthTitle}>
              {name} is {month.percentClosed}% closed.
            </h2>
            <Progress value={month.percentClosed} tone="good" label={`${name} progress`} />
            <p className="muted num">
              {formatNumber(matchedCount)} of {formatNumber(month.transactionsTotal)} transactions matched so far.
            </p>
          </div>
        )}

        {!closed && month.remaining.length > 0 ? (
          <div className={styles.left}>
            <h3 className="h3">What’s left</h3>
            <ul className={styles.leftList}>
              {month.remaining.map((r) => (
                <li key={r.id}>
                  <Dot tone={r.tone} />
                  <span className={styles.leftText}>
                    {r.text}
                    {r.href ? (
                      <>
                        {" "}
                        <Link href={r.href} className="link">
                          {r.linkLabel ?? "Open"}
                        </Link>
                      </>
                    ) : null}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}

        <div className={styles.statsBlock}>
          {!closed ? <h3 className="h3">So far</h3> : null}
          <dl className={styles.stats}>
            {(closed ? stats : stats.slice(0, 4)).map((s) => (
              <div key={s.label} className={styles.stat}>
                <dt className={styles.statLabel}>{s.label}</dt>
                <dd className={`${styles.statValue} num ${s.good ? "good-text" : ""}`}>{formatNumber(s.value)}</dd>
              </div>
            ))}
          </dl>
        </div>

        {closed ? (
          <p className={styles.spent}>
            You spent <span className="num">{month.stats.minutesSpent}</span>{" "}
            {plural(month.stats.minutesSpent, "minute", "minutes")}.
          </p>
        ) : null}
      </section>

      {month.matched.length > 0 ? (
        <section aria-labelledby="matched-h">
          <div className="section-head">
            <h2 id="matched-h" className="h2">
              Matched in {name}
            </h2>
            <span className="meta">A few recent ones</span>
          </div>
          <ul className="card list">
            {month.matched.map((m) => (
              <li key={m.id} className={styles.match}>
                <div className={styles.matchRow}>
                  <span className={styles.matchIcon}>
                    <Icon name="document" size={18} />
                  </span>
                  <span className={styles.matchMain}>
                    <span className={styles.matchName}>{m.supplier}</span>
                    <span className="meta">
                      {m.description} · {formatDayShort(m.date)}
                    </span>
                  </span>
                  <span className={`${styles.matchAmount} num`}>{formatMoney(m.amount, m.currency)}</span>
                </div>
                <Disclosure summary="Why?" className={styles.matchWhy}>
                  <CheckList items={m.reasons} />
                </Disclosure>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
