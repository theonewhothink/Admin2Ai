import type { Metadata } from "next";
import { NeedsYouList } from "@/components/needs/NeedsYouList";
import { getCompanies, getNeedsYou } from "@/lib/api";

export const metadata: Metadata = { title: "Needs you" };

export default async function NeedsYouPage() {
  const [items, companies] = await Promise.all([getNeedsYou(), getCompanies()]);
  const companyNames = Object.fromEntries(companies.map((c) => [c.id, c.name]));
  return (
    <div className="container-narrow page">
      <NeedsYouList items={items} companyNames={companyNames} />
    </div>
  );
}
