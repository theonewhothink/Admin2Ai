import { Icon, type IconName } from "@/components/Icon";
import { Status } from "@/components/ui";
import type { SourceItem, SourceStatus, SourcesData } from "@/lib/types";

const groupIcon: Record<string, IconName> = {
  email: "mail",
  banks: "bank",
  cards: "payment",
  accountant: "users",
  suppliers: "building",
  insurance: "shield",
  investments: "flag",
  lenders: "document",
  government: "document",
};

const statusLabel: Record<SourceStatus, { tone: "good" | "attention" | "risk"; label: string } | null> = {
  healthy: { tone: "good", label: "Connected" },
  stale: { tone: "attention", label: "Needs reconnecting" },
  not_connected: { tone: "attention", label: "Not connected" },
  hold: { tone: "risk", label: "Payment on hold" },
  known: null,
};

const dateFmt = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "Europe/Lisbon" });
const fmt = (iso?: string | null) => (iso ? dateFmt.format(new Date(iso)) : "");

function extra(item: SourceItem): string {
  const parts: string[] = [];
  if (item.renewsOn) parts.push(`Renews ${fmt(item.renewsOn)}`);
  if (item.lastSeen) parts.push(`Last payment ${fmt(item.lastSeen)}`);
  if (item.foundIn) parts.push(`Found in: ${item.foundIn}`);
  return parts.join(" · ");
}

export function SourcesView({ data }: { data: SourcesData }) {
  const connected = data.groups
    .filter((g) => ["email", "banks", "cards", "accountant"].includes(g.id))
    .reduce((n, g) => n + g.items.length, 0);
  const learned = data.groups
    .filter((g) => !["email", "banks", "cards", "accountant"].includes(g.id))
    .reduce((n, g) => n + g.items.length, 0);

  return (
    <div className="container-narrow page">
      <header className="page-head">
        <h1 className="h1">Sources</h1>
        <p className="lead">
          {connected} connections I read from, and {learned} companies and organisations I have learned about.
        </p>
      </header>

      <nav aria-label="Jump to" style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 24 }}>
        {data.groups.map((g) => (
          <a key={g.id} href={`#${g.id}`} className="meta" style={{ padding: "4px 10px", borderRadius: 999, background: "var(--card)", textDecoration: "none" }}>
            {g.title} <span className="tabular">{g.items.length}</span>
          </a>
        ))}
      </nav>

      <div className="stack-6">
        {data.groups.map((g) => (
          <section key={g.id} id={g.id} aria-labelledby={`${g.id}-h`}>
            <div className="section-head">
              <h2 id={`${g.id}-h`} className="h2">
                {g.title}
              </h2>
              <span className="meta">{g.description}</span>
            </div>
            {g.items.length === 0 ? (
              <p className="card card-pad meta">Nothing yet.</p>
            ) : (
              <ul className="card list">
                {g.items.map((item) => {
                  const s = statusLabel[item.status];
                  const more = extra(item);
                  return (
                    <li key={item.id} className="list-row">
                      <Icon name={groupIcon[g.id] ?? "document"} size={20} style={{ color: "var(--text-2)", flexShrink: 0 }} />
                      <span style={{ flex: 1, minWidth: 0, display: "grid", gap: 2 }}>
                        <span style={{ fontWeight: 600, overflowWrap: "anywhere" }}>{item.name}</span>
                        <span className="meta" style={{ overflowWrap: "anywhere" }}>
                          {[item.company, item.detail].filter(Boolean).join(" · ")}
                        </span>
                        {more ? <span className="meta" style={{ overflowWrap: "anywhere" }}>{more}</span> : null}
                      </span>
                      {s ? <Status tone={s.tone} label={s.label} /> : null}
                    </li>
                  );
                })}
              </ul>
            )}
          </section>
        ))}
      </div>
    </div>
  );
}
