import Link from "next/link";
import styles from "./accountant.module.css";
import { FirmName } from "./FirmName";
import { InviteClient } from "./InviteClient";
import { Icon } from "@/components/Icon";
import { Progress } from "@/components/ui";
import type { AccountantClientRow } from "@/lib/types";

export function AccountantHomeView({ clients }: { clients: AccountantClientRow[] }) {
  const ready = clients.filter((c) => c.complete === 100).length;
  const waitingOnYou = clients.reduce((n, c) => n + c.needsAccountant, 0);
  const missing = clients.reduce((n, c) => n + c.missing, 0);
  const month = clients[0]?.month ?? "";

  return (
    <div className="container page">
      <header className="page-head">
        <p className="meta">
          <FirmName />
        </p>
        <h1 className="h1">Clients</h1>
        <p className="lead">
          {month ? `${month}. ` : ""}
          {clients.length} {clients.length === 1 ? "client" : "clients"}, {ready} ready to export.
        </p>
      </header>

      <dl className={styles.summary}>
        <div className="card">
          <dt>Ready to export</dt>
          <dd className="num">{ready}</dd>
        </div>
        <div className="card">
          <dt>Documents still missing</dt>
          <dd className="num">{missing}</dd>
        </div>
        <div className="card">
          <dt>Waiting for you</dt>
          <dd className="num">{waitingOnYou}</dd>
        </div>
      </dl>

      <div className={`card ${styles.tableCard}`}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th scope="col">Client</th>
              <th scope="col">Month</th>
              <th scope="col" className={styles.completeCol}>
                Complete
              </th>
              <th scope="col" className={styles.numCol}>
                Missing
              </th>
              <th scope="col" className={styles.numCol}>
                Needs accountant
              </th>
              <th scope="col">
                <span className="visually-hidden">Open</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {clients.map((c) => (
              <tr key={c.id}>
                <th scope="row">
                  <Link href={`/accountant/${encodeURIComponent(c.id)}`} className={styles.rowLink}>
                    {c.name}
                  </Link>
                  {c.business && c.business !== c.name ? <span className={`meta ${styles.business}`}>{c.business}</span> : null}
                </th>
                <td className={`muted ${styles.monthCell}`}>{c.month}</td>
                <td className={styles.completeCell}>
                  <span className={styles.complete}>
                    <Progress value={c.complete} tone="good" label={`${c.name} complete`} />
                    <span className="num">{c.complete}%</span>
                  </span>
                </td>
                <td data-label="Missing" className={`${styles.numCol} num ${c.missing === 0 ? styles.zero : ""}`}>
                  {c.missing}
                </td>
                <td data-label="Needs accountant" className={styles.numCol}>
                  {c.needsAccountant > 0 ? (
                    <span className="pill pill-attention num">{c.needsAccountant}</span>
                  ) : (
                    <span className={`num ${styles.zero}`}>0</span>
                  )}
                </td>
                <td className={styles.chevCell}>
                  <Icon name="chevronRight" size={18} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {clients.length === 0 ? <p className={`${styles.empty} ${styles.tableEmpty}`}>No clients yet. Invite one below.</p> : null}
      </div>

      <div className={styles.below}>
        <InviteClient />
      </div>
    </div>
  );
}
