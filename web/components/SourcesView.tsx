"use client";

import { useState } from "react";
import { Icon, type IconName } from "@/components/Icon";
import { Status } from "@/components/ui";
import { addSource, downloadFile, getSources, removeSource } from "@/lib/api";
import { formatMonth, localDay } from "@/lib/format";
import { production } from "@/lib/mode";
import type { SourceItem, SourceStatus, SourcesData } from "@/lib/types";

/** "2026-09" for any day in October 2026. */
function lastMonth(today: string): string {
  const [year, month] = today.split("-").map(Number) as [number, number];
  return month === 1 ? `${year - 1}-12` : `${year}-${String(month - 1).padStart(2, "0")}`;
}

const groupIcon: Record<string, IconName> = {
  email: "mail",
  banks: "bank",
  cards: "payment",
  accountant: "users",
  portals: "globe",
  files: "folder",
  accounting: "briefcase",
  suppliers: "building",
  insurance: "shield",
  investments: "flag",
  lenders: "document",
  government: "document",
};

/** Which "add" form each group opens (the accountant is invited from onboarding). */
const addKind: Record<string, string> = {
  email: "email",
  banks: "bank",
  cards: "card",
  portals: "portal",
  files: "files",
  accounting: "accounting",
  suppliers: "supplier",
  insurance: "insurance",
  investments: "investment",
  lenders: "loan",
  government: "government",
};

/** The provider a form starts with, for the kinds that have one. */
const firstProvider: Record<string, string> = { email: "google", files: "google", accounting: "toconline" };

/**
 * What each form sends besides `kind`: only the fields of the chosen provider, so a key typed for one
 * accounting program is never sent when another is chosen.
 */
function fieldsFor(kind: string, provider: string): string[] {
  switch (kind) {
    case "email":
      return ["provider", "address", "companyId", ...(provider === "imap" ? ["host", "password"] : [])];
    case "files":
      return ["provider", "address", "folder", "companyId", ...(provider === "microsoft" ? ["drive"] : [])];
    case "accounting":
      if (provider === "invoicexpress") return ["provider", "account", "apiKey", "companyId"];
      if (provider === "toconline") return ["provider", "clientId", "clientSecret", "oauthUrl", "apiUrl", "companyId"];
      return ["provider", "companyId"];
    case "portal":
      return ["supplier", "username", "password", "companyId"];
    case "bank":
      return ["bank", "iban", "companyId"];
    case "card":
      return ["last4", "bank", "companyId"];
    case "supplier":
      return ["name", "taxId", "email"];
    default:
      return ["name", "detail", "renewsOn", "companyId"];
  }
}

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

const CONNECTED = ["email", "banks", "cards", "accountant", "portals", "files", "accounting"];

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "grid", gap: 6 }}>
      <span className="label">{label}</span>
      {children}
    </label>
  );
}

function AddForm({
  kind,
  companies,
  onDone,
  onCancel,
}: {
  kind: string;
  companies: SourcesData["companies"];
  onDone: (message: string) => void;
  onCancel: () => void;
}) {
  // Email, cloud storage and supplier websites can serve every company; the rest belong to one.
  const allCompanies = ["email", "files", "portal"].includes(kind);
  const [v, setV] = useState<Record<string, string>>({
    provider: firstProvider[kind] ?? "",
    companyId: allCompanies ? "" : (companies[0]?.id ?? ""),
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setV({ ...v, [k]: e.target.value });
  const text = (k: string, label: string, props: React.InputHTMLAttributes<HTMLInputElement> = {}) => (
    <Field label={label}>
      <input className="input" value={v[k] ?? ""} onChange={set(k)} {...props} />
    </Field>
  );
  const secret = (k: string, label: string) => text(k, label, { type: "password", required: true, autoComplete: "new-password" });
  const signIn =
    (kind === "email" && v.provider !== "imap") || kind === "files" || (kind === "accounting" && v.provider === "moloni");
  const company = (optional: boolean) => (
    <Field label="Company">
      <select className="input" value={v.companyId ?? ""} onChange={set("companyId")}>
        {optional ? <option value="">All companies</option> : null}
        {companies.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </select>
    </Field>
  );

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    const body: Record<string, unknown> = { kind };
    for (const k of fieldsFor(kind, v.provider ?? "")) {
      const raw = v[k] ?? "";
      const value = k === "password" ? raw : raw.trim(); // a password is sent exactly as typed
      if (value) body[k] = value;
    }
    const result = await addSource(body);
    setBusy(false);
    if (result.authorizeUrl) {
      window.location.assign(result.authorizeUrl); // provider consent; comes back signed in
      return;
    }
    if (!result.ok) {
      setError(result.message ?? "I couldn't add that.");
      return;
    }
    onDone(result.message ?? "Done.");
  }

  return (
    <form onSubmit={submit} className="card card-pad" style={{ display: "grid", gap: 16, marginTop: 12 }}>
      {kind === "email" ? (
        <>
          <Field label="Provider">
            <select className="input" value={v.provider} onChange={set("provider")}>
              <option value="google">Google (Gmail, Workspace)</option>
              <option value="microsoft">Microsoft (Outlook, 365)</option>
              <option value="imap">Other provider (IMAP)</option>
            </select>
          </Field>
          {text("address", "Email address", { type: "email", required: true, placeholder: "invoices@yourcompany.pt" })}
          {v.provider === "imap" ? (
            <>
              {text("host", "Mail server", { required: true, placeholder: "imap.yourcompany.pt" })}
              {text("password", "App password", { type: "password", required: true, autoComplete: "new-password" })}
            </>
          ) : null}
          {company(true)}
          <p className="meta">
            {v.provider === "imap"
              ? "The password is encrypted and only used to read this mailbox."
              : "You sign in once with your provider. I keep an encrypted, read-only sign-in and renew it myself."}
          </p>
        </>
      ) : null}
      {kind === "bank" ? (
        <>
          {text("bank", "Bank", { required: true, placeholder: "Novo Banco" })}
          {text("iban", "IBAN (optional)", { placeholder: "PT50 …", inputMode: "text" })}
          {company(false)}
          <p className="meta">Banks require you to confirm access every 180 days. I remind you a week before.</p>
        </>
      ) : null}
      {kind === "card" ? (
        <>
          {text("last4", "Last 4 digits", { required: true, inputMode: "numeric", maxLength: 4, pattern: "\\d{4}" })}
          {text("bank", "Issued by", { required: true, placeholder: "Revolut" })}
          {company(false)}
        </>
      ) : null}
      {kind === "portal" ? (
        <>
          {text("supplier", "Supplier", { required: true, placeholder: "Vodafone" })}
          {text("username", "Your username on their website", { required: true, autoComplete: "off" })}
          {secret("password", "Password")}
          {company(true)}
          <p className="meta">
            The password is encrypted and only used to sign in there. If the website sends you a code, I will ask you for it in
            Needs you.
          </p>
        </>
      ) : null}
      {kind === "files" ? (
        <>
          <Field label="Where your files are">
            <select className="input" value={v.provider} onChange={set("provider")}>
              <option value="google">Google Drive</option>
              <option value="microsoft">OneDrive or SharePoint</option>
            </select>
          </Field>
          {text("address", v.provider === "microsoft" ? "Microsoft account" : "Google account", {
            type: "email",
            required: true,
            placeholder: "invoices@yourcompany.pt",
          })}
          {v.provider === "microsoft" ? (
            <>
              {text("folder", "Folder to watch (optional)", { placeholder: "/Invoices/2026" })}
              {text("drive", "SharePoint library (optional)", { placeholder: "sites/…/drive" })}
            </>
          ) : (
            text("folder", "Folder to watch (optional)", { placeholder: "Paste the folder’s link from Google Drive" })
          )}
          {company(true)}
          <p className="meta">
            {v.provider === "microsoft"
              ? "You sign in once with Microsoft. Leave the library empty to use your own OneDrive."
              : "You sign in once with Google."}{" "}
            I search it for missing invoices and only read, never change, your files.
          </p>
        </>
      ) : null}
      {kind === "accounting" ? (
        <>
          <Field label="Accounting software">
            <select className="input" value={v.provider} onChange={set("provider")}>
              <option value="toconline">TOConline</option>
              <option value="moloni">Moloni</option>
              <option value="invoicexpress">InvoiceXpress</option>
            </select>
          </Field>
          {v.provider === "invoicexpress" ? (
            <>
              {text("account", "Account name", { required: true, placeholder: "yourcompany", autoComplete: "off" })}
              {secret("apiKey", "Access key")}
              <p className="meta">
                The account name is the first part of your InvoiceXpress address. The access key is in your InvoiceXpress
                account settings. It is encrypted and never shown again.
              </p>
            </>
          ) : null}
          {v.provider === "toconline" ? (
            <>
              {text("clientId", "Client identifier", { required: true, autoComplete: "off" })}
              {secret("clientSecret", "Client secret")}
              {text("oauthUrl", "Sign-in address", { required: true, type: "url", placeholder: "https://app….toconline.pt/oauth" })}
              {text("apiUrl", "Data address", { required: true, type: "url", placeholder: "https://api….toconline.pt" })}
              <p className="meta">
                Copy all four from TOConline, in Empresa › Configurações › Dados API. The secret is encrypted and never shown
                again.
              </p>
            </>
          ) : null}
          {v.provider === "moloni" ? <p className="meta">You sign in to Moloni once. I only read your documents there.</p> : null}
          {company(false)}
        </>
      ) : null}
      {kind === "supplier" ? (
        <>
          {text("name", "Supplier", { required: true })}
          {text("taxId", "Tax number (optional)")}
          {text("email", "Invoice email (optional)", { type: "email" })}
        </>
      ) : null}
      {["insurance", "investment", "loan", "government"].includes(kind) ? (
        <>
          {text("name", "Name", { required: true })}
          {text("detail", "What it is (optional)", { placeholder: kind === "insurance" ? "Car insurance · €40 a month" : "" })}
          {kind === "insurance" ? text("renewsOn", "Renews on (optional)", { type: "date" }) : null}
          {company(false)}
        </>
      ) : null}
      {error ? (
        <p role="alert" style={{ color: "var(--risk)" }}>
          {error}
        </p>
      ) : null}
      <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
        <button className="btn btn-primary" type="submit" disabled={busy}>
          {busy ? "Adding…" : signIn ? "Sign in and connect" : "Add"}
        </button>
        <button className="btn btn-quiet" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </form>
  );
}

export function SourcesView({ data: initial }: { data: SourcesData }) {
  const [data, setData] = useState(initial);
  const [adding, setAdding] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const previous = lastMonth(localDay(new Date().toISOString()));

  async function refresh(message: string) {
    setAdding(null);
    setNotice(message);
    setData(await getSources());
  }

  /** The month as the accounting software has it (production: the server asks it now), as a ZIP file. */
  async function exportMonth(item: SourceItem) {
    setExporting(item.id);
    const failed = await downloadFile(
      `/api/accounting/${encodeURIComponent(item.id)}/export?month=${previous}`,
      `${item.name.replace(/[^0-9A-Za-z_-]+/g, "-")}-${previous}.zip`, // as the server names it
      `I couldn’t get ${formatMonth(previous)} from ${item.name}. Try again in a moment.`,
    );
    setExporting(null);
    setNotice(failed);
  }

  async function remove(item: SourceItem) {
    if (!window.confirm(`Remove ${item.name}? I will stop reading it and delete its sign-in.`)) return;
    const result = await removeSource(item.id);
    await refresh(result.message ?? (result.ok ? "Removed." : "I couldn't remove that."));
  }

  const count = (ids: string[], inside: boolean) =>
    data.groups.filter((g) => ids.includes(g.id) === inside).reduce((n, g) => n + g.items.length, 0);

  return (
    <div className="container-narrow page">
      <header className="page-head">
        <h1 className="h1">Sources</h1>
        <p className="lead">
          {count(CONNECTED, true)} connections I read from, and {count(CONNECTED, false)} companies and organisations I have
          learned about. Add anything I should search.
        </p>
      </header>

      {notice ? (
        <p role="status" className="card card-pad" style={{ marginBottom: 24 }}>
          {notice}
        </p>
      ) : null}

      <nav aria-label="Jump to" style={{ display: "flex", flexWrap: "wrap", gap: 8, marginBottom: 24 }}>
        {data.groups.map((g) => (
          <a
            key={g.id}
            href={`#${g.id}`}
            className="meta"
            style={{ padding: "4px 10px", borderRadius: 999, background: "var(--surface)", textDecoration: "none" }}
          >
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
              {addKind[g.id] ? (
                <button className="btn btn-quiet" type="button" onClick={() => setAdding(adding === g.id ? null : g.id)}>
                  + Add
                </button>
              ) : null}
            </div>
            <p className="meta" style={{ marginBottom: 12 }}>
              {g.description}
            </p>
            {adding === g.id ? (
              <AddForm kind={addKind[g.id] ?? ""} companies={data.companies} onDone={refresh} onCancel={() => setAdding(null)} />
            ) : null}
            {g.items.length === 0 ? (
              <p className="card card-pad meta">Nothing yet.</p>
            ) : (
              <ul className="card list" style={{ marginTop: adding === g.id ? 12 : 0 }}>
                {g.items.map((item) => {
                  const s = statusLabel[item.status];
                  const more = extra(item);
                  return (
                    <li key={item.id} className="list-row" style={{ alignItems: "flex-start" }}>
                      <Icon name={groupIcon[g.id] ?? "document"} size={20} style={{ color: "var(--text-2)", flexShrink: 0, marginTop: 2 }} />
                      <span style={{ flex: 1, minWidth: 0, display: "grid", gap: 2 }}>
                        <span style={{ fontWeight: 600, overflowWrap: "anywhere" }}>{item.name}</span>
                        <span className="meta" style={{ overflowWrap: "anywhere" }}>
                          {[...new Set([item.company, item.detail].filter(Boolean))].join(" · ")}
                        </span>
                        {more ? (
                          <span className="meta" style={{ overflowWrap: "anywhere" }}>
                            {more}
                          </span>
                        ) : null}
                        {item.signIn ? (
                          <span className="meta" style={{ overflowWrap: "anywhere" }}>
                            {item.signIn}
                          </span>
                        ) : null}
                      </span>
                      <span style={{ display: "grid", justifyItems: "end", gap: 4 }}>
                        {s ? <Status tone={s.tone} label={s.label} /> : null}
                        {g.id === "accounting" && production && item.status === "healthy" ? (
                          <button
                            className="btn btn-quiet"
                            type="button"
                            disabled={exporting === item.id}
                            onClick={() => void exportMonth(item)}
                            aria-label={`Download ${formatMonth(previous)} from ${item.name}`}
                          >
                            {exporting === item.id ? "Preparing…" : `Download ${formatMonth(previous)}`}
                          </button>
                        ) : null}
                        {g.id !== "accountant" ? (
                          <button className="btn btn-quiet" type="button" onClick={() => void remove(item)} aria-label={`Remove ${item.name}`}>
                            Remove
                          </button>
                        ) : null}
                      </span>
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
