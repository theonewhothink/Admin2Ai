import type { Metadata } from "next";
import { CompaniesView } from "@/components/companies/CompaniesView";
import { LiveCompanies } from "@/components/live/pages";
import { browserEngine, getCompanies, getMonth } from "@/lib/api";

export const metadata: Metadata = { title: "Your businesses" };

export default async function CompaniesPage() {
  if (browserEngine) return <LiveCompanies />;
  const companies = await getCompanies();
  const months = await Promise.all(companies.map((c) => getMonth(c.id, c.currentMonth)));
  return <CompaniesView companies={companies} months={months} />;
}
