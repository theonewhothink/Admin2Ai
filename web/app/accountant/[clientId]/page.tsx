import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { AccountantClientView } from "@/components/accountant/AccountantClientView";
import { LiveAccountantClient } from "@/components/live/pages";
import {
  browserEngine,
  clientRendered,
  getAccountantClient,
  getAccountantClients,
  getCompanies,
  hasApi,
  production,
} from "@/lib/api";

type Props = { params: Promise<{ clientId: string }> };

/**
 * Client ids are only known at run time with a backend (production ids are `<business>~<company>`), so
 * with one the page loads its client in the browser, like production; the static site pre-builds one
 * page per client (the sample clients and every demo company the engine knows).
 */
const loadsInBrowser = clientRendered || hasApi;

export async function generateStaticParams() {
  if (!browserEngine) return [];
  const [clients, companies] = await Promise.all([getAccountantClients(), getCompanies()]);
  return [...new Set([...clients.map((c) => c.id), ...companies.map((c) => c.id)])].map((clientId) => ({ clientId }));
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  // Production data needs the signed-in session, which only the browser has; a backend's live data loads there too.
  if (production || (hasApi && !browserEngine)) return { title: "Client" };
  const { clientId } = await params;
  const client = await getAccountantClient(clientId);
  return { title: client?.name ?? "Client" };
}

export default async function AccountantClientPage({ params }: Props) {
  const { clientId } = await params;
  if (loadsInBrowser) return <LiveAccountantClient id={clientId} />;
  const client = await getAccountantClient(clientId);
  if (!client) notFound();
  return <AccountantClientView client={client} />;
}
