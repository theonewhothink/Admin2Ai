import Link from "next/link";
import styles from "./accountant.module.css";
import { EvidenceChip, EvidenceChips } from "./EvidenceChip";
import { ExportButton } from "./ExportButton";
import { TeachRule } from "./TeachRule";
import { Icon } from "@/components/Icon";
import { Bullets, CheckList, Disclosure, Dot, Progress } from "@/components/ui";
import { formatDayShort, formatMoney } from "@/lib/format";
import type { AccountantClientDetail, ReconciliationRow, Tone } from "@/lib/types";

const PILL: Record<Tone, string> = { good: "pill-good", attention: "pill-attention", risk: "pill-risk", neutral: "" };

function Reconciliation({ rows }: { rows: ReconciliationRow[] }) {
  if (rows.length === 0) return <p className={styles.empty}>No payments this month yet.</p>;
  return (
    <ul className={styles.recon}>
      {rows.map((r) => {
        const proven = r.status === "closed" || r.status === "not_required";
        return (
          <li key={r.id} className={styles.reconRow}>
            <div className={styles.reconHead}>
              <span className={`meta num ${styles.reconDate}`}>{formatDayShort(r.date)}</span>
              <span className={styles.reconPayee}>
                <span className={styles.itemTitle}>{r.payee}</span>
                <span className={`meta ${styles.reconDesc}`}>{r.description}</span>
              </span>
              <span className={`num ${styles.reconAmount}`}>
                {r.direction === "in" ? "+" : "−"}
                {formatMoney(r.amount, r.currency)}
              </span>
              <span className={`pill ${PILL[r.tone]}`}>{r.statusLabel}</span>
            </div>
            {r.documents.length > 0 ? (
              <ul className={styles.evidenceList} aria-label={`Documents for ${r.payee}`}>
                {r.documents.map((d) => (
                  <li key={d.id}>
                    {d.href ? (
                      <EvidenceChip link={{ id: d.evidenceId ?? d.id, label: d.label, href: d.href, kind: "document" }} />
                    ) : (
                      <span className="chip">
                        <Icon name="document" size={16} />
                        {d.label}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            ) : null}
            {r.why.length > 0 || r.evidence.length > 0 ? (
              <Disclosure summary="Why?">
                <div className="stack-2">
                  {r.why.length > 0 && proven ? <CheckList items={r.why} /> : null}
                  {r.why.length > 0 && !proven ? <Bullets items={r.why} /> : null}
                  <EvidenceChips links={r.evidence} label={`Evidence for ${r.payee}`} />
                </div>
              </Disclosure>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export function AccountantClientView({ client }: { client: AccountantClientDetail }) {
  const exp = client.exportState;
  const reconciliation = client.reconciliation ?? [];
  const evidenceLinks = client.evidenceLinks ?? [];
  const missing = client.missingDocuments ?? [];
  const openReasons = client.openReasons ?? [];
  const matched = reconciliation.filter((r) => r.status === "closed" || r.status === "not_required").length;

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
          {client.accountant ? ` · Accountant: ${client.accountant.firm}` : ""}
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
            <ExportButton
              label={exp.state === "ready" ? `Download for ${client.software}` : "Download what’s ready"}
              software={client.software}
              companyId={client.id}
              period={client.period ? { from: client.period.from, to: client.period.to } : undefined}
              path={client.links?.export}
            />
          ) : null}
        </section>

        {client.reconciliation ? (
          <section className={`card ${styles.panel} ${styles.fullRow}`} aria-labelledby="recon-h">
            <div className={styles.panelHead}>
              <h2 id="recon-h" className="h3">
                Reconciliation
              </h2>
              <span className="num muted">
                {matched} of {reconciliation.length} proven
              </span>
            </div>
            <Reconciliation rows={reconciliation} />
          </section>
        ) : null}

        {client.reconciliation ? (
          <section className={`card ${styles.panel}`} aria-labelledby="open-h">
            <h2 id="open-h" className="h3">
              Still open
            </h2>
            {missing.length > 0 || openReasons.length > 0 ? (
              <ul className={styles.items}>
                {missing.map((m) => (
                  <li key={m.id}>
                    <Dot tone="attention" />
                    <div className={styles.itemBody}>
                      <span className={styles.itemTitle}>
                        {m.payee}
                        {m.amount !== null ? ` · ${formatMoney(m.amount, m.currency)}` : ""} ·{" "}
                        <span className="num">{formatDayShort(m.date)}</span>
                      </span>
                      <span className={styles.itemDetail}>{m.plan}</span>
                    </div>
                  </li>
                ))}
                {openReasons.map((reason) => (
                  <li key={reason}>
                    <Dot tone="neutral" />
                    <div className={styles.itemBody}>
                      <span className={styles.itemDetail}>{reason}</span>
                    </div>
                  </li>
                ))}
              </ul>
            ) : (
              <p className={styles.empty}>Nothing is open for {client.month}.</p>
            )}
          </section>
        ) : null}

        {client.evidenceLinks ? (
          <section className={`card ${styles.panel}`} aria-labelledby="originals-h">
            <div className={styles.panelHead}>
              <h2 id="originals-h" className="h3">
                Originals
              </h2>
              <span className="num muted">{evidenceLinks.length}</span>
            </div>
            {evidenceLinks.length > 0 ? (
              <EvidenceChips links={evidenceLinks} label="Originals for this month" />
            ) : (
              <p className={styles.empty}>No originals for {client.month} yet.</p>
            )}
          </section>
        ) : null}

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
                    {q.evidence && q.evidence.length > 0 ? (
                      <EvidenceChips links={q.evidence} label={`Evidence for “${q.question}”`} />
                    ) : null}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.empty}>No open questions.</p>
          )}
        </section>

        <div className={styles.fullRow}>
          <TeachRule clientName={client.name} path={client.links?.rules} existing={client.rules ?? []} />
        </div>
      </div>
    </div>
  );
}
