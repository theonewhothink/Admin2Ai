import Link from "next/link";
import { CostCenters } from "@/components/companies/CostCenters";
import { MonthView } from "@/components/companies/MonthView";
import styles from "@/components/companies/companies.module.css";
import { CompanyStatus } from "@/components/CompanyStatus";
import { Icon } from "@/components/Icon";
import type { CompanySummary, MonthClose, MonthKey } from "@/lib/types";
import { formatMonth, formatMonthShort, formatMonthYear } from "@/lib/format";

export function CompanyView({
  company,
  monthKey,
  month,
}: {
  company: CompanySummary;
  monthKey: MonthKey;
  month: MonthClose | null;
}) {
  const monthOptions = company.months.includes(monthKey) ? company.months : [monthKey, ...company.months];

  return (
    <div className="container-narrow page">
      <div className={styles.back}>
        <Link href="/companies" className="link-quiet">
          <Icon name="chevronLeft" size={16} />
          Your businesses
        </Link>
      </div>

      <header className={styles.head}>
        <div className={styles.headMain}>
          <h1 className="h1">{company.name}</h1>
          <p className="meta">
            {company.legalName} · <span className="num">{company.taxId}</span>
          </p>
        </div>
        <CompanyStatus company={company} />
      </header>

      <nav aria-label="Month" className={styles.monthNav}>
        <div className="segmented">
          {monthOptions.map((m) => (
            <Link
              key={m}
              href={m === company.currentMonth ? `/companies/${company.id}` : `/companies/${company.id}?month=${m}`}
              aria-current={m === monthKey ? "page" : undefined}
              aria-label={formatMonthYear(m)}
              scroll={false}
            >
              {formatMonthShort(m)}
            </Link>
          ))}
        </div>
      </nav>

      <div className="stack-6">
        {month ? (
          <MonthView month={month} />
        ) : (
          <div className="card card-pad">
            <p className="h3">Nothing to show for {formatMonth(monthKey)} yet.</p>
            <p className="muted">I start a month as soon as its first transaction arrives.</p>
          </div>
        )}
        <CostCenters companyId={company.id} />
      </div>
    </div>
  );
}
