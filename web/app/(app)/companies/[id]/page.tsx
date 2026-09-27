import type { Metadata } from "next";
import Link from "next/link";
import { notFound } from "next/navigation";
import { MonthView } from "@/components/companies/MonthView";
import styles from "@/components/companies/companies.module.css";
import { CompanyStatus } from "@/components/CompanyStatus";
import { Icon } from "@/components/Icon";
import { getCompany, getMonth } from "@/lib/api";
import { formatMonth, formatMonthShort, formatMonthYear } from "@/lib/format";

type Props = {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { id } = await params;
  const company = await getCompany(id);
  return { title: company?.name ?? "Company" };
}

export default async function CompanyPage({ params, searchParams }: Props) {
  const [{ id }, query] = await Promise.all([params, searchParams]);
  const company = await getCompany(id);
  if (!company) notFound();

  const requested = typeof query.month === "string" && /^\d{4}-\d{2}$/.test(query.month) ? query.month : null;
  const monthKey = requested ?? company.currentMonth;
  const month = await getMonth(company.id, monthKey);
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

      {month ? (
        <MonthView month={month} />
      ) : (
        <div className="card card-pad">
          <p className="h3">Nothing to show for {formatMonth(monthKey)} yet.</p>
          <p className="muted">I start a month as soon as its first transaction arrives.</p>
        </div>
      )}
    </div>
  );
}
