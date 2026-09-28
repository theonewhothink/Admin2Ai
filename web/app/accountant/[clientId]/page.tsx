import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { AccountantClientView } from "@/components/accountant/AccountantClientView";
import { LiveAccountantClient } from "@/components/live/pages";
import { browserEngine, getAccountantClient, getAccountantClients, getCompanies } from "@/lib/api";

type Props = { params: Promise<{ clientId: string }> };

/** The static site pre-builds one page per client: the sample clients and every demo company the engine knows. */
export async function generateStaticParams() {
  if (!browserEngine) return [];
  const [clients, companies] = await Promise.all([getAccountantClients(), getCompanies()]);
  return [...new Set([...clients.map((c) => c.id), ...companies.map((c) => c.id)])].map((clientId) => ({ clientId }));
}

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  const { clientId } = await params;
  const client = await getAccountantClient(clientId);
  return { title: client?.name ?? "Client" };
}

export default async function AccountantClientPage({ params }: Props) {
  const { clientId } = await params;
  if (browserEngine) return <LiveAccountantClient id={clientId} />;
  const client = await getAccountantClient(clientId);
  if (!client) notFound();
  return <AccountantClientView client={client} />;
}
