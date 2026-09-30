import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { Suspense } from "react";
import { CompanyView } from "@/components/companies/CompanyView";
import { Loading } from "@/components/live/Loading";
import { LiveCompany } from "@/components/live/pages";
import { browserEngine, clientRendered, getCompanies, getCompany, getMonth, production } from "@/lib/api";

type Props = {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ [key: string]: string | string[] | undefined }>;
};

/** The static site pre-builds one page per company (the engine's demo uses the same ids as the sample data). */
export async function generateStaticParams() {
  if (!browserEngine) return [];
  return (await getCompanies()).map((c) => ({ id: c.id }));
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  // Production data needs the owner's session, which only the browser has.
  if (production) return { title: "Your business" };
  const { id } = await params;
  const company = await getCompany(id);
  return { title: company?.name ?? "Company" };
}

export default async function CompanyPage({ params, searchParams }: Props) {
  if (clientRendered) {
    const { id } = await params;
    return (
      <Suspense fallback={<Loading />}>
        <LiveCompany id={id} />
      </Suspense>
    );
  }
  const [{ id }, query] = await Promise.all([params, searchParams]);
  const company = await getCompany(id);
  if (!company) notFound();

  const requested = typeof query.month === "string" && /^\d{4}-\d{2}$/.test(query.month) ? query.month : null;
  const monthKey = requested ?? company.currentMonth;
  const month = await getMonth(company.id, monthKey);
  return <CompanyView company={company} monthKey={monthKey} month={month} />;
}
