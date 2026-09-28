"use client";

/**
 * Pages as they run on the static site (NEXT_PUBLIC_ENGINE=browser): the data
 * is loaded in the browser from the in-browser engine, then handed to the same
 * views the server renders in the other modes.
 */
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { AccountantClientView } from "@/components/accountant/AccountantClientView";
import { AccountantHomeView } from "@/components/accountant/AccountantHomeView";
import { ActivityView } from "@/components/ActivityView";
import { AskClient } from "@/components/ask/AskClient";
import { CompaniesView } from "@/components/companies/CompaniesView";
import { CompanyView } from "@/components/companies/CompanyView";
import { AuditView } from "@/components/flow/AuditView";
import { HomeView } from "@/components/home/HomeView";
import { NeedsYouList } from "@/components/needs/NeedsYouList";
import { SettingsView } from "@/components/SettingsView";
import { SourcesView } from "@/components/SourcesView";
import {
  getAccountantClient,
  getAccountantClients,
  getActivity,
  getAudit,
  getCompanies,
  getCompany,
  getHome,
  getMonth,
  getNeedsYou,
  getSources,
} from "@/lib/api";
import type { MonthKey } from "@/lib/types";
import { Loading } from "./Loading";
import { useData } from "./useData";

const MONTH = /^\d{4}-\d{2}$/;

const loadHome = () => Promise.all([getHome(), getNeedsYou()]);

export function LiveHome() {
  const params = useSearchParams();
  const data = useData(loadHome);
  if (!data) return <Loading narrow={false} />;
  const [home, needs] = data;
  return <HomeView home={home} needsIds={needs.map((n) => n.id)} demoStale={params.get("demo") === "stale"} />;
}

const loadNeeds = () => Promise.all([getNeedsYou(), getCompanies()]);

export function LiveNeedsYou() {
  const data = useData(loadNeeds);
  if (!data) return <Loading />;
  const [items, companies] = data;
  const companyNames = Object.fromEntries(companies.map((c) => [c.id, c.name]));
  return (
    <div className="container-narrow page">
      <NeedsYouList items={items} companyNames={companyNames} />
    </div>
  );
}

export function LiveActivity() {
  const feed = useData(getActivity);
  return feed ? <ActivityView feed={feed} /> : <Loading />;
}

async function loadCompanies() {
  const companies = await getCompanies();
  const months = await Promise.all(companies.map((c) => getMonth(c.id, c.currentMonth)));
  return { companies, months };
}

export function LiveCompanies() {
  const data = useData(loadCompanies);
  return data ? <CompaniesView companies={data.companies} months={data.months} /> : <Loading />;
}

async function loadCompany(id: string, requested: string | null) {
  const company = await getCompany(id);
  if (!company) return { company: null, monthKey: "", month: null };
  const monthKey: MonthKey = requested ?? company.currentMonth;
  return { company, monthKey, month: await getMonth(company.id, monthKey) };
}

function Missing({ what, href, back }: { what: string; href: string; back: string }) {
  return (
    <div className="container-narrow page">
      <div className="card card-pad stack-2">
        <p className="h3">I couldn’t find that {what}.</p>
        <div>
          <Link href={href} className="btn btn-secondary">
            {back}
          </Link>
        </div>
      </div>
    </div>
  );
}

export function LiveCompany({ id }: { id: string }) {
  const month = useSearchParams().get("month");
  const data = useData(loadCompany, id, month && MONTH.test(month) ? month : null);
  if (!data) return <Loading />;
  if (!data.company) return <Missing what="business" href="/companies" back="Your businesses" />;
  return <CompanyView company={data.company} monthKey={data.monthKey} month={data.month} />;
}

export function LiveSettings() {
  const home = useData(getHome);
  return home ? <SettingsView home={home} /> : <Loading />;
}

export function LiveAudit() {
  const audit = useData(getAudit);
  return audit ? <AuditView audit={audit} /> : <Loading narrow={false} />;
}

export function LiveAccountantHome() {
  const clients = useData(getAccountantClients);
  return clients ? <AccountantHomeView clients={clients} /> : <Loading narrow={false} />;
}

const loadClient = async (id: string) => ({ client: await getAccountantClient(id) });

export function LiveAccountantClient({ id }: { id: string }) {
  const data = useData(loadClient, id);
  if (!data) return <Loading narrow={false} />;
  if (!data.client) return <Missing what="client" href="/accountant" back="Clients" />;
  return <AccountantClientView client={data.client} />;
}

export function LiveAsk({ examples }: { examples: string[] }) {
  const q = useSearchParams().get("q");
  const initialQuestion = q && q.trim() ? q.trim().slice(0, 500) : undefined;
  return <AskClient key={initialQuestion ?? "empty"} initialQuestion={initialQuestion} examples={examples} />;
}

export function LiveSources() {
  const data = useData(getSources);
  if (!data) return <Loading />;
  return <SourcesView data={data} />;
}
