import type { Metadata } from "next";
import { Icon, type IconName } from "@/components/Icon";
import { Status } from "@/components/ui";
import { getHome } from "@/lib/api";
import { owner } from "@/lib/data";
import type { ConnectionKind } from "@/lib/types";

export const metadata: Metadata = { title: "Settings" };

const kindIcon: Record<ConnectionKind, IconName> = { email: "mail", bank: "bank", accountant: "users" };

const remembered = [
  "IKEA paid with card •••• 4817 goes to Hazel Tree.",
  "Mercadona on card •••• 2210 is personal.",
  "The €1,200 monthly transfer to M. García is Hazel Tree’s studio rent.",
  "Adobe is software for Company C.",
];

export default async function SettingsPage() {
  const home = await getHome();
  return (
    <div className="container-narrow page">
      <header className="page-head">
        <h1 className="h1">Settings</h1>
        <p className="lead">
          {owner.fullName} · {owner.email}
        </p>
      </header>

      <div className="stack-6">
        <section aria-labelledby="conn-h">
          <div className="section-head">
            <h2 id="conn-h" className="h2">
              Connections
            </h2>
          </div>
          <ul className="card list">
            {home.connections.map((c) => (
              <li key={c.id} className="list-row">
                <Icon name={kindIcon[c.kind]} size={20} style={{ color: "var(--text-2)" }} />
                <span style={{ flex: 1, minWidth: 0, display: "grid" }}>
                  <span style={{ fontWeight: 600 }}>{c.name}</span>
                  <span className="meta">{c.account}</span>
                </span>
                <Status tone={c.status === "healthy" ? "good" : "attention"} label={c.status === "healthy" ? "Connected" : "Needs reconnecting"} />
              </li>
            ))}
          </ul>
        </section>

        <section aria-labelledby="rules-h">
          <div className="section-head">
            <h2 id="rules-h" className="h2">
              Things I remember
            </h2>
            <span className="meta">From your answers</span>
          </div>
          <ul className="card list">
            {remembered.map((r) => (
              <li key={r} className="list-row">
                <Icon name="bookmark" size={18} style={{ color: "var(--text-2)" }} />
                <span style={{ flex: 1 }}>{r}</span>
              </li>
            ))}
          </ul>
        </section>

        <section aria-labelledby="notify-h">
          <div className="section-head">
            <h2 id="notify-h" className="h2">
              When I contact you
            </h2>
          </div>
          <div className="card card-pad stack-2">
            <label className="checkbox">
              <input type="checkbox" defaultChecked />
              <span>Only when I need an answer, or a payment looks risky.</span>
            </label>
            <label className="checkbox">
              <input type="checkbox" defaultChecked />
              <span>A short summary when a month is closed.</span>
            </label>
            <label className="checkbox">
              <input type="checkbox" />
              <span>A weekly note of everything I handled.</span>
            </label>
          </div>
        </section>
      </div>
    </div>
  );
}
