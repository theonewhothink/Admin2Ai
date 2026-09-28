import Link from "next/link";
import styles from "./accountant.module.css";
import { ExportButton } from "./ExportButton";
import { TeachRule } from "./TeachRule";
import { Icon } from "@/components/Icon";
import { Dot, Progress } from "@/components/ui";
import type { AccountantClientDetail } from "@/lib/types";

export function AccountantClientView({ client }: { client: AccountantClientDetail }) {
  const exp = client.exportState;
  const existingRules = client.id === "hazel-tree" ? ["Rent to M. García is office rent (account 621)."] : [];

  return (
    <div className="container page">
      <div className={styles.back}>
        <Link href="/accountant" className="link-quiet">
          <Icon name="chevronLeft" size={16} />
          Clients
        </Link>
      </div>

      <header className={styles.clientHead}>
        <h1 className="h1">{client.name}</h1>
        <p className="meta">
          <span className="num">{client.taxId}</span> · {client.month} · Exports to {client.software}
        </p>
      </header>

      <div className={styles.grid}>
        <section className={`card ${styles.panel}`} aria-labelledby="evidence-h">
          <div className={styles.panelHead}>
            <h2 id="evidence-h" className="h3">
              Evidence
            </h2>
            <span className="num muted">{client.complete}% complete</span>
          </div>
          <Progress value={client.complete} tone="good" label="Complete" />
          <dl className={styles.facts}>
            {client.evidence.map((e) => (
              <div key={e.label}>
                <dt>{e.label}</dt>
                <dd className="num">{e.value}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className={`card ${styles.panel}`} aria-labelledby="export-h">
          <div className={styles.panelHead}>
            <h2 id="export-h" className="h3">
              Export
            </h2>
            {exp.state === "exported" ? (
              <span className="pill pill-good">Exported</span>
            ) : exp.state === "ready" ? (
              <span className="pill pill-good">Ready</span>
            ) : (
              <span className="pill">Partly ready</span>
            )}
          </div>
          <p className={styles.itemDetail}>{exp.note}</p>
          {exp.state !== "exported" ? (
            <div>
              <ExportButton
                label={exp.state === "ready" ? `Export to ${client.software}` : `Export what’s ready`}
                software={client.software}
                count={exp.ready}
              />
            </div>
          ) : null}
        </section>

        <section className={`card ${styles.panel}`} aria-labelledby="anomalies-h">
          <h2 id="anomalies-h" className="h3">
            Anomalies
          </h2>
          {client.anomalies.length > 0 ? (
            <ul className={styles.items}>
              {client.anomalies.map((a) => (
                <li key={a.id}>
                  <Dot tone={a.tone} />
                  <div className={styles.itemBody}>
                    <span className={styles.itemTitle}>{a.title}</span>
                    <span className={styles.itemDetail}>{a.detail}</span>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.empty}>Nothing unusual this month.</p>
          )}
        </section>

        <section className={`card ${styles.panel}`} aria-labelledby="tax-h">
          <h2 id="tax-h" className="h3">
            Tax flags
          </h2>
          {client.taxFlags.length > 0 ? (
            <ul className={styles.items}>
              {client.taxFlags.map((t) => (
                <li key={t.id}>
                  <Dot tone="attention" />
                  <div className={styles.itemBody}>
                    <span className={styles.itemTitle}>{t.title}</span>
                    <span className={styles.itemDetail}>{t.detail}</span>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.empty}>No tax questions this month.</p>
          )}
        </section>

        <section className={`card ${styles.panel} ${styles.fullRow}`} aria-labelledby="questions-h">
          <h2 id="questions-h" className="h3">
            Questions
          </h2>
          {client.questions.length > 0 ? (
            <ul className={styles.items}>
              {client.questions.map((q) => (
                <li key={q.id}>
                  <Dot tone={q.status === "answered" ? "good" : "neutral"} />
                  <div className={styles.itemBody}>
                    <span className={styles.itemTitle}>{q.question}</span>
                    <span className={styles.itemDetail}>
                      {q.status === "answered" ? q.answer : "Waiting for the client. I will remind them tomorrow if needed."}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.empty}>No open questions.</p>
          )}
        </section>

        <div className={styles.fullRow}>
          <TeachRule clientName={client.name} existing={existingRules} />
        </div>
      </div>
    </div>
  );
}
