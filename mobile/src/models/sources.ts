/**
 * Sources (the owner's request: "a tab that shows all the sources the system is feeding from"): what I read, the
 * one plain line each gave, and the companies they feed. Never green while a connection is not read or anything is
 * still open (§47): the server's tone says so, and a source that is not connected makes it amber here too.
 */
import { copy } from "../copy";
import type { SourcesData, SourceStatus, Tone } from "../api/types";

/** The groups that are read, in the order the owner thinks of them (what was learned is on the web). */
const READ = ["email", "banks", "cards", "files", "accounting", "portals", "accountant"] as const;

const COUNTED: { id: string; one: string; many: string }[] = [
  { id: "email", one: "mailbox", many: "mailboxes" },
  { id: "banks", one: "bank account", many: "bank accounts" },
  { id: "cards", one: "card", many: "cards" },
  { id: "files", one: "cloud storage account", many: "cloud storage accounts" },
  { id: "accounting", one: "accounting program", many: "accounting programs" },
  { id: "portals", one: "supplier website", many: "supplier websites" },
];

export interface SourceRow {
  id: string;
  name: string;
  where: string;
  line: string | null;
  status: { label: string; tone: Tone } | null;
}

export interface SourcesView {
  /** "What I read: 1 mailbox, 3 bank accounts, 4 cards" (Home's row). */
  reads: string;
  summary: string;
  coverage: { text: string; tone: Tone };
  sections: { id: string; title: string; rows: SourceRow[] }[];
  companies: { id: string; name: string; tax: string; sources: string }[];
}

const STATUS: Record<SourceStatus, { label: string; tone: Tone } | null> = {
  healthy: { label: copy.connections.healthy, tone: "good" },
  stale: { label: copy.connections.stale, tone: "attention" },
  not_connected: { label: copy.sources.notConnected, tone: "attention" },
  hold: { label: copy.sources.onHold, tone: "risk" },
  known: null,
};

/** "1 mailbox, 3 bank accounts, 4 cards" from what is read; "" when nothing is. */
export function readsLine(data: SourcesData): string {
  return COUNTED.map(({ id, one, many }) => {
    const n = data.groups.find((g) => g.id === id)?.items.length ?? 0;
    return n ? `${n} ${n === 1 ? one : many}` : null;
  })
    .filter((p): p is string => p !== null)
    .join(", ");
}

export function buildSourcesView(data: SourcesData): SourcesView {
  const sections = READ.map((id) => data.groups.find((g) => g.id === id))
    .filter((g): g is NonNullable<typeof g> => Boolean(g && g.items.length))
    .map((g) => ({
      id: g.id,
      title: g.title,
      rows: g.items.map((item) => ({
        id: item.id,
        name: item.name,
        where: [...new Set([item.company, item.detail].filter(Boolean))].join(" · "),
        line: item.coverage ?? null,
        status: STATUS[item.status],
      })),
    }));
  const notRead = sections.some((s) => s.rows.some((r) => r.status && r.status.tone !== "good"));
  const reads = readsLine(data);
  return {
    reads: reads ? `${copy.sources.whatIRead}: ${reads}` : copy.sources.nothingRead,
    summary: data.summary,
    coverage: { text: data.coverage, tone: data.tone === "good" && !notRead ? "good" : "attention" },
    sections,
    companies: data.companies.map((c) => ({
      id: c.id,
      name: c.name,
      tax: c.taxId ? `${c.taxIdLabel} ${c.taxId}` : "",
      sources: c.sources.length ? c.sources.join(", ") : copy.sources.nothingForCompany,
    })),
  };
}
