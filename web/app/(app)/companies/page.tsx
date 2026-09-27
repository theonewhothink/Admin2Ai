import type { Metadata } from "next";
import Link from "next/link";
import { CompanyStatus } from "@/components/CompanyStatus";
import styles from "@/components/companies/companies.module.css";
import { Icon } from "@/components/Icon";
import { Progress } from "@/components/ui";
import { getCompanies, getMonth } from "@/lib/api";
import { countWord, formatMonth } from "@/lib/format";

export const metadata: Metadata = { title: "Your businesses" };

export default async function CompaniesPage() {
  const companies = await getCompanies();
  const months = await Promise.all(companies.map((c) => getMonth(c.id, c.currentMonth)));
  const n = companies.length;

  return (
    <div className="container-narrow page">
      <header className="page-head">
        <h1 className="h1">Your businesses</h1>
        <p className="lead">
          {n === 1 ? "One company." : `${countWord(n).replace(/^./, (c) => c.toUpperCase())} companies.`} I keep each one
          separate, and tell you when something could belong to another.
        </p>
      </header>

      <ul className={styles.companyList}>
        {companies.map((c, i) => {
          const m = months[i];
          return (
            <li key={c.id}>
              <Link href={`/companies/${c.id}`} className={`card card-link ${styles.companyCard}`}>
                <div className={styles.companyTop}>
                  <div className="stack-1">
                    <span className={styles.companyName}>{c.name}</span>
                    <span className="meta">
                      {c.legalName} · <span className="num">{c.taxId}</span>
                    </span>
                  </div>
                  <CompanyStatus company={c} />
                </div>
                {m ? (
                  <div className={styles.companyMonth}>
                    <span>{formatMonth(m.month)}</span>
                    <Progress value={m.percentClosed} tone="good" label={`${c.name}, ${formatMonth(m.month)}`} />
                    <span className="num">{m.status === "closed" ? "Closed" : `${m.percentClosed}%`}</span>
                  </div>
                ) : (
                  <p className="meta">{c.detail}</p>
                )}
              </Link>
            </li>
          );
        })}
      </ul>

      <div style={{ marginTop: "var(--s-3)" }}>
        <Link href="/onboarding" className={`link-quiet ${styles.addLink}`}>
          <Icon name="building" size={18} />
          Add another business
        </Link>
      </div>
    </div>
  );
}
