import Link from "next/link";
import { AskBox } from "@/components/AskBox";
import { CompanyStatus } from "@/components/CompanyStatus";
import { ConnectionBanner } from "@/components/ConnectionBanner";
import { Headline, NeedsTile } from "@/components/home/HomeLive";
import styles from "@/components/home/home.module.css";
import { Icon } from "@/components/Icon";
import { Dot, Progress } from "@/components/ui";
import { liveData } from "@/lib/api";
import type { HomeData } from "@/lib/types";
import { staleConnections } from "@/lib/data";
import { formatDayShort } from "@/lib/format";

/** Home. Rendered on the server (API or sample data) or in the browser (in-browser engine). */
export function HomeView({ home, needsIds, demoStale }: { home: HomeData; needsIds: string[]; demoStale: boolean }) {
  const connections = demoStale ? staleConnections : home.connections;
  const staleCount = connections.filter((c) => c.status === "stale").length;
  const stale = connections.find((c) => c.status === "stale");
  const nextDue = [...home.dueSoon].sort((a, b) => a.due.localeCompare(b.due))[0];
  const { currentMonth } = home;
  const monthDone = currentMonth.percentClosed === 100;

  return (
    <div className="container page">
      {stale ? <ConnectionBanner connection={stale} /> : null}

      <div className={styles.home}>
        {/* The engine's headline counts its own stale connections; the ?demo=stale preview adds sample ones. */}
        <Headline
          greeting={home.greeting}
          needsIds={needsIds}
          staleCount={staleCount}
          headline={demoStale ? undefined : home.headline}
        />

        <div className={styles.askDock}>
          <AskBox />
        </div>

        <section aria-label="At a glance" className="tiles">
          <NeedsTile needsIds={needsIds} staleCount={staleCount} />
          <Link href="#coming-up" className="card card-link tile">
            <span className="tile-label">
              <Dot tone="neutral" />
              Due soon
            </span>
            <span className="tile-value num">{home.dueSoon.length}</span>
          </Link>
          <Link href="/companies" className="card card-link tile">
            <span className="tile-label">
              <Dot tone={monthDone ? "good" : "neutral"} />
              {currentMonth.label}
            </span>
            <span className={styles.tileSub}>
              <span className="tile-value num">
                {currentMonth.percentClosed}%<small>closed</small>
              </span>
              {/* Green means closed: only at 100%. Anything less is still open (amber). */}
              <Progress
                value={currentMonth.percentClosed}
                tone={monthDone ? "good" : "attention"}
                label={`${currentMonth.label} closed`}
              />
            </span>
          </Link>
        </section>

        <div className={styles.columns}>
          <div className="stack-4">
            <section aria-labelledby="companies-h">
              <div className="section-head">
                <h2 id="companies-h" className="h2">
                  Your businesses
                </h2>
                <Link href="/companies" className="link-quiet">
                  All
                  <Icon name="chevronRight" size={16} />
                </Link>
              </div>
              <ul className="card list">
                {home.companies.map((c) => (
                  <li key={c.id}>
                    <Link href={`/companies/${c.id}`} className={`list-row ${styles.companyRow}`}>
                      <span className={styles.rowMain}>
                        <span className={styles.companyName}>{c.name}</span>
                        <span className="meta">{c.detail}</span>
                      </span>
                      <CompanyStatus company={c} />
                      <Icon name="chevronRight" size={18} className={styles.chev} />
                    </Link>
                  </li>
                ))}
              </ul>
            </section>

            <section aria-labelledby="coming-up-h" id="coming-up">
              <div className="section-head">
                <h2 id="coming-up-h" className="h2">
                  Coming up
                </h2>
                {liveData ? (
                  <Link href="/deadlines" className="link-quiet">
                    All deadlines
                    <Icon name="chevronRight" size={16} />
                  </Link>
                ) : nextDue ? (
                  <span className="meta">Next on {formatDayShort(nextDue.due)}</span>
                ) : null}
              </div>
              <ul className="card list">
                {home.dueSoon.map((d) => {
                  const [day, month] = formatDayShort(d.due).split(" ");
                  const inner = (
                    <>
                      <span className={styles.dueDate} aria-hidden="true">
                        <span className={`${styles.dueDay} num`}>{day}</span>
                        <span className={styles.dueMonth}>{month}</span>
                      </span>
                      <span className={styles.rowMain}>
                        <span className={styles.dueTitle}>{d.title}</span>
                        <span className="meta">
                          {d.companyName} · {d.note}
                        </span>
                      </span>
                      <span className="visually-hidden">Due {formatDayShort(d.due)}.</span>
                      <Dot tone={d.tone} />
                    </>
                  );
                  return (
                    <li key={d.id}>
                      {d.href ? (
                        <Link href={d.href} className="list-row">
                          {inner}
                        </Link>
                      ) : (
                        <div className="list-row">{inner}</div>
                      )}
                    </li>
                  );
                })}
              </ul>
            </section>
          </div>

          <section aria-labelledby="handled-h">
            <div className="section-head">
              <h2 id="handled-h" className="h2">
                Handled for you
              </h2>
              <span className="meta">{home.handledPeriodLabel}</span>
            </div>
            <div className="card">
              <ul className={styles.handled}>
                {home.handled.map((h) => (
                  <li key={h.id} className={styles.handledRow}>
                    <span className={`${styles.handledCount} num`}>{h.count}</span>
                    <span className={styles.handledLabel}>{h.label}</span>
                  </li>
                ))}
              </ul>
              <div className={styles.cardFoot}>
                <Link href="/activity" className="link-quiet">
                  See everything I did
                  <Icon name="chevronRight" size={16} />
                </Link>
              </div>
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
