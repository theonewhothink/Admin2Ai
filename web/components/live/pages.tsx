"use client";

/**
 * Pages as they run when the data loads in the browser: on the static site
 * (NEXT_PUBLIC_ENGINE=browser) from the in-browser engine, and in production
 * from the API with the owner's session. The data is handed to the same views
 * the server renders in the other modes.
 */
import dynamic from "next/dynamic";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { AccountantClientView } from "@/components/accountant/AccountantClientView";
import { AccountantHomeView } from "@/components/accountant/AccountantHomeView";
import { ActivityView } from "@/components/ActivityView";
import { ChatClient } from "@/components/ask/ChatClient";
import { CompaniesView } from "@/components/companies/CompaniesView";
import { CompanyView } from "@/components/companies/CompanyView";
import { DiagramView } from "@/components/diagram/DiagramView";
import { AuditView } from "@/components/flow/AuditView";
import { HomeView } from "@/components/home/HomeView";
import { NeedsYouList } from "@/components/needs/NeedsYouList";
import { SettingsView } from "@/components/SettingsView";
import { useSession } from "@/components/session/context";
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
  getPipeline,
  getSources,
} from "@/lib/api";
import { production } from "@/lib/mode";
import { ownerFrom } from "@/lib/owner";
import type { MonthKey, Pipeline } from "@/lib/types";
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

// Production only: loaded on demand so the demo never ships account code in its pages.
const AccountSection = dynamic(() => import("@/components/auth/AccountSection").then((m) => m.AccountSection));

export function LiveSettings() {
  const home = useData(getHome);
  const session = useSession();
  if (!home) return <Loading />;
  if (!production) return <SettingsView home={home} />;
  return <SettingsView home={home} who={session ? ownerFrom(session) : null} account={<AccountSection />} />;
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
  return <ChatClient key={initialQuestion ?? "empty"} initialQuestion={initialQuestion} examples={examples} />;
}

export function LiveSources() {
  const data = useData(getSources);
  if (!data) return <Loading />;
  return <SourcesView data={data} />;
}

export function LiveDiagram() {
  const data = useData(getPipeline);
  if (!data) return <Loading narrow={false} />;
  return (
    <>
      <DiagramLead data={data} />
      <DiagramView data={data} />
    </>
  );
}

export function DiagramLead({ data }: { data: Pipeline }) {
  const s = data.summary;
  return (
    <p className="lead" style={{ marginBottom: 24 }}>
      {s.items} items so far: {s.closed} closed with proof, {s.open} in progress
      {s.waiting ? `, ${s.waiting} waiting for you` : ""}
      {s.notRequired ? `, ${s.notRequired} that need no document` : ""}.
    </p>
  );
}
