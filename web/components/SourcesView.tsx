"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { openChat } from "@/components/ask/open-chat";
import { Icon, type IconName } from "@/components/Icon";
import { useData } from "@/components/live/useData";
import { Dot, Status } from "@/components/ui";
import {
  addSource,
  call,
  downloadFile,
  getSourcePayments,
  getSources,
  removeSource,
  understandSource,
} from "@/lib/api";
import { formatDayShort, formatMoney, formatMonth, localDay } from "@/lib/format";
import { production } from "@/lib/mode";
import type {
  PaymentState,
  SourceItem,
  SourceStatus,
  SourcesData,
  Tone,
  UnderstoodSource,
} from "@/lib/types";
import styles from "./sources.module.css";

/** "2026-09" for any day in October 2026. */
function lastMonth(today: string): string {
  const [year, month] = today.split("-").map(Number) as [number, number];
  return month === 1 ? `${year - 1}-12` : `${year}-${String(month - 1).padStart(2, "0")}`;
}

const groupIcon: Record<string, IconName> = {
  email: "mail",
  banks: "bank",
  cards: "card",
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

/** Where the business is read from, in the order the owner thinks of them. */
const READ = ["email", "banks", "cards", "files", "accounting", "portals", "accountant"];
/** What was learned from them: suppliers, insurance, investments, loans, tax and government. */
const LEARNED = ["suppliers", "insurance", "investments", "lenders", "government"];

/** What the engine says about a demo connection (its sign-in is simulated). */
const SIMULATED = "Demo connection: no real sign-in was made.";

/** Plug and play: one tap opens the form for each kind of place to read. */
const TILES: { kind: string; label: string; icon: IconName }[] = [
  { kind: "email", label: "Email", icon: "mail" },
  { kind: "bank", label: "Bank account", icon: "bank" },
  { kind: "card", label: "Card", icon: "card" },
  { kind: "files", label: "Cloud storage", icon: "folder" },
  { kind: "accounting", label: "Accounting software", icon: "briefcase" },
  { kind: "portal", label: "Supplier website", icon: "globe" },
];

/** The form a kind understood from "Something missing?" opens. */
const FORM_FOR: Record<Exclude<UnderstoodSource["kind"], "ask">, string> = {
  email: "email",
  bank: "bank",
  card: "card",
  files: "files",
  accounting: "accounting",
  portal: "portal",
};

/** The provider a form starts with, for the kinds that have one. */
const firstProvider: Record<string, string> = { email: "google", files: "google", accounting: "toconline" };

const singular: Record<string, string> = {
  suppliers: "a supplier",
  insurance: "insurance",
  investments: "an investment",
  lenders: "a loan",
  government: "a tax or government office",
};

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

/** Emerald only for what is settled; amber while I'm looking or need the owner. */
const stateTone: Record<PaymentState, Tone> = {
  proven: "good",
  not_needed: "good",
  looking: "attention",
  needs_you: "attention",
  personal: "neutral",
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

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "grid", gap: 6 }}>
      <span className="label" style={{ marginBottom: 0 }}>
        {label}
      </span>
      {children}
    </label>
  );
}

function AddForm({
  kind,
  companies,
  initial,
  onDone,
  onCancel,
}: {
  kind: string;
  companies: SourcesData["companies"];
  /** What "Something missing?" understood: the fields it fills in. */
  initial?: Record<string, string>;
  onDone: (message: string) => void;
  onCancel: () => void;
}) {
  // Email, cloud storage and supplier websites can serve every company; the rest belong to one.
  const allCompanies = ["email", "files", "portal"].includes(kind);
  const [v, setV] = useState<Record<string, string>>(() => ({
    provider: initial && "provider" in initial ? (initial.provider ?? "") : (firstProvider[kind] ?? ""),
    companyId: allCompanies ? "" : (companies[0]?.id ?? ""),
    ...(initial ?? {}),
  }));
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
    (kind === "email" && v.provider !== "imap" && v.provider !== "") ||
    kind === "files" ||
    (kind === "accounting" && v.provider === "moloni");
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
    <form onSubmit={submit} className="card card-pad" style={{ display: "grid", gap: 16 }} aria-label="Add a source">
      {kind === "email" ? (
        <>
          <Field label="Provider">
            <select className="input" value={v.provider} onChange={set("provider")} required>
              {v.provider === "" ? (
                <option value="" disabled>
                  Choose the provider
                </option>
              ) : null}
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

/** One bank account's or card's payments, each with where it stands: the proof, payment by payment. */
function Payments({ item, id }: { item: SourceItem; id: string }) {
  const loaded = useData(getSourcePayments, item.id);
  if (!loaded) {
    return (
      <p id={id} className={`${styles.payments} ${styles.paymentsNote}`} role="status">
        Loading the payments…
      </p>
    );
  }
  if (!loaded.data) {
    return (
      <p id={id} className={`${styles.payments} ${styles.paymentsNote}`} role="status">
        {loaded.message}
      </p>
    );
  }
  if (loaded.data.items.length === 0) {
    return (
      <p id={id} className={`${styles.payments} ${styles.paymentsNote}`}>
        No payments yet.
      </p>
    );
  }
  return (
    <ul id={id} className={styles.payments} aria-label={`Payments of ${item.name}`}>
      {loaded.data.items.map((p) => (
        <li key={p.id} className={styles.payment} data-state={p.state}>
          <Link href={p.detailHref} className={styles.paymentMain}>
            <span className={styles.paymentWho}>{p.merchant}</span>
            <span className={`num ${styles.paymentAmount}`}>
              {p.direction === "in" ? "+" : ""}
              {formatMoney(p.amount, p.currency)}
            </span>
          </Link>
          <div className={styles.paymentState}>
            <span className={styles.stateLine}>
              <Dot tone={stateTone[p.state]} />
              {p.state === "needs_you" ? (
                <Link href={p.href} className="link">
                  {p.stateText}
                </Link>
              ) : (
                <span className={styles.stateText}>{p.stateText}</span>
              )}
            </span>
            <span className={`num ${styles.paymentDate}`}>{formatDayShort(p.date)}</span>
          </div>
          {p.companyName && p.companyName !== item.company ? <span className={styles.paymentFor}>For {p.companyName}</span> : null}
        </li>
      ))}
    </ul>
  );
}

export function SourcesView({ data: initial }: { data: SourcesData }) {
  const [data, setData] = useState(initial);
  const [adding, setAdding] = useState<{ kind: string; place: string; fields?: Record<string, string>; n: number } | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const [reconnecting, setReconnecting] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [missing, setMissing] = useState("");
  const [understanding, setUnderstanding] = useState(false);
  const [said, setSaid] = useState<string | null>(null);
  const previous = lastMonth(localDay(new Date().toISOString()));
  const summary = data.summary;
  const group = (id: string) => data.groups.find((g) => g.id === id);

  async function refresh(message: string) {
    setAdding(null);
    setSaid(null);
    setNotice(message);
    setData(await getSources());
  }

  /** Open the add form for `kind` (a tile, a group's + Add, or what "Something missing?" understood). */
  const openForm = (kind: string, place: string, fields?: Record<string, string>) =>
    setAdding((a) => (a && a.kind === kind && a.place === place && !fields ? null : { kind, place, fields, n: (a?.n ?? 0) + 1 }));

  async function understand(e: React.FormEvent) {
    e.preventDefault();
    await understandText(missing);
  }

  async function understandText(typed: string) {
    const text = typed.trim();
    if (!text || understanding) return;
    setUnderstanding(true);
    setNotice(null);
    const r = await understandSource(text);
    setUnderstanding(false);
    if (!r.ok) {
      setSaid(r.message);
      return;
    }
    const u = r.understood;
    setSaid(u.message);
    if (u.kind === "ask") {
      setAdding(null);
      openChat(text);
      return;
    }
    if (u.already) {
      setAdding(null);
      return;
    }
    openForm(FORM_FOR[u.kind], "missing", u.fields);
  }

  // Sent here with something to add (the phone's "Add it on the web": /sources?missing=…): understood at once.
  useEffect(() => {
    const given = new URLSearchParams(window.location.search).get("missing")?.trim().slice(0, 500);
    if (!given) return;
    const start = async () => {
      setMissing(given);
      await understandText(given);
    };
    void start();
    // Once, on arrival.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  /** Reconnect: a Google or Microsoft sign-in opens the provider's page; the rest is tried again. */
  async function reconnect(item: SourceItem) {
    const id = item.connectionId ?? item.id;
    setReconnecting(item.id);
    const r = await call<{ authorizeUrl?: string; redirectUrl?: string; message?: string }>(
      "POST",
      `/api/connections/${encodeURIComponent(id)}/reconnect`,
      {},
    );
    const go = r.body.authorizeUrl ?? r.body.redirectUrl;
    if (r.ok && typeof go === "string" && /^https?:\/\//.test(go)) {
      window.location.assign(go);
      return;
    }
    setReconnecting(null);
    await refresh(r.body.message ?? (r.ok ? "Done. It is connected again." : "I couldn’t reconnect it. Try again in a moment."));
  }

  const form = (place: string) =>
    adding && adding.place === place ? (
      <AddForm
        key={`${adding.kind}-${adding.n}`}
        kind={adding.kind}
        companies={data.companies}
        initial={adding.fields}
        onDone={refresh}
        onCancel={() => setAdding(null)}
      />
    ) : null;

  const reading = READ.map(group).filter((g) => g && g.items.length > 0);
  // The demo's connections are simulated: said once for all of them, not on every card.
  const simulated = reading.some((g) => g?.items.some((i) => i.signIn === SIMULATED));
  const learned = LEARNED.map(group).filter((g): g is NonNullable<typeof g> => Boolean(g));

  return (
    <div className="container-narrow page">
      <header className="page-head">
        <h1 className="h1">Sources</h1>
        <p className="lead">{summary?.text ?? "Everything I read for your companies, and what it gave."}</p>
      </header>

      {summary?.coverage ? (
        <div className={`notice notice-${summary.tone} ${styles.verdict}`} role="status" data-tone={summary.tone}>
          <Dot tone={summary.tone} />
          <p>{summary.coverage}</p>
        </div>
      ) : null}

      {notice ? (
        <p role="status" className="card card-pad" style={{ marginBottom: 24 }}>
          {notice}
        </p>
      ) : null}

      <div className="stack-6">
        <section id="missing" className={styles.section} aria-labelledby="missing-h">
          <h2 id="missing-h" className="h2">
            Something missing?
          </h2>
          <form className={styles.ask} onSubmit={understand}>
            <label htmlFor="missing-text" className="visually-hidden">
              What I’m not reading yet
            </label>
            <textarea
              id="missing-text"
              className={`input ${styles.askText}`}
              value={missing}
              rows={1}
              onChange={(e) => setMissing(e.target.value.replace(/\n/g, " "))}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  e.currentTarget.form?.requestSubmit();
                }
              }}
              placeholder="Tell me what I’m not reading yet: an email address, a bank, a card…"
              maxLength={500}
              autoComplete="off"
              spellCheck={false}
            />
            <button className="btn btn-primary" type="submit" disabled={understanding || !missing.trim()}>
              {understanding ? "Reading…" : "Add it"}
            </button>
          </form>
          {said ? (
            <p className={styles.said} role="status">
              <Icon name="ask" size={18} />
              <span>{said}</span>
            </p>
          ) : null}
          {form("missing")}
          <p className="meta">Or choose what to add:</p>
          <div className={styles.tiles}>
            {TILES.map((t) => (
              <button
                key={t.kind}
                type="button"
                className={styles.tile}
                aria-pressed={adding?.place === "tiles" && adding.kind === t.kind}
                onClick={() => {
                  setSaid(null);
                  openForm(t.kind, "tiles");
                }}
              >
                <Icon name={t.icon} size={20} />
                {t.label}
              </button>
            ))}
          </div>
          {form("tiles")}
        </section>

        <section id="read" className={styles.section} aria-labelledby="read-h">
          <h2 id="read-h" className="h2">
            What I read
          </h2>
          {simulated ? <p className={`meta ${styles.sectionLead}`}>These are demo connections: no real sign-in was made.</p> : null}
          {reading.length === 0 ? <p className="card card-pad meta">Nothing yet. Add your email and your bank above.</p> : null}
          {reading.map((g) =>
            g ? (
              <div key={g.id} id={g.id} className={styles.group}>
                <h3 className={styles.groupTitle}>
                  {g.title} <span className="num">{g.items.length}</span>
                </h3>
                <ul className="card list">
                  {g.items.map((item) => {
                    const s = statusLabel[item.status];
                    const hasPayments = g.id === "banks" || g.id === "cards";
                    const expanded = open === item.id;
                    const listId = `payments-${item.id}`;
                    const where = [...new Set([item.company, item.detail].filter(Boolean))].join(" · ");
                    return (
                      <li key={item.id} className={styles.source} data-source={item.id}>
                        <div className={styles.sourceRow}>
                          <Icon name={groupIcon[g.id] ?? "document"} size={20} className={styles.sourceIcon} />
                          <div className={styles.sourceMain}>
                            <div className={styles.sourceTop}>
                              <span className={styles.name}>{item.name}</span>
                              {s ? <Status tone={s.tone} label={s.label} /> : null}
                            </div>
                            {where ? <span className="meta">{where}</span> : null}
                            {item.coverage ? <p className={styles.coverage}>{item.coverage.text}</p> : null}
                            {item.signIn && item.signIn !== SIMULATED ? <p className={styles.signIn}>{item.signIn}</p> : null}
                          </div>
                        </div>
                        <div className={styles.actions}>
                          {hasPayments ? (
                            <button
                              type="button"
                              className={styles.expand}
                              aria-expanded={expanded}
                              aria-controls={listId}
                              onClick={() => setOpen(expanded ? null : item.id)}
                            >
                              {expanded ? "Hide payments" : "Show payments"}
                              <Icon name="chevronDown" size={16} />
                            </button>
                          ) : null}
                          {item.status === "stale" ? (
                            <button
                              className="btn btn-secondary"
                              type="button"
                              disabled={reconnecting === item.id}
                              onClick={() => void reconnect(item)}
                            >
                              {reconnecting === item.id ? "Reconnecting…" : "Reconnect"}
                            </button>
                          ) : null}
                          {g.id === "accounting" && production && item.status === "healthy" ? (
                            <button
                              className="btn btn-secondary"
                              type="button"
                              disabled={exporting === item.id}
                              onClick={() => void exportMonth(item)}
                              aria-label={`Download ${formatMonth(previous)} from ${item.name}`}
                            >
                              {exporting === item.id ? "Preparing…" : `Download ${formatMonth(previous)}`}
                            </button>
                          ) : null}
                          {g.id !== "accountant" ? (
                            <button
                              className={`${styles.removeQuiet} ${styles.push}`}
                              type="button"
                              onClick={() => void remove(item)}
                              aria-label={`Remove ${item.name}`}
                            >
                              Remove
                            </button>
                          ) : null}
                        </div>
                        {hasPayments && expanded ? <Payments item={item} id={listId} /> : null}
                      </li>
                    );
                  })}
                </ul>
              </div>
            ) : null,
          )}
        </section>

        {data.companies.length > 0 ? (
          <section id="companies" className={styles.section} aria-labelledby="companies-h">
            <h2 id="companies-h" className="h2">
              Companies I cover
            </h2>
            <ul className="card list">
              {data.companies.map((c) => (
                <li key={c.id} className={styles.company} data-company={c.id}>
                  <div className={styles.companyHead}>
                    <span className={styles.name}>{c.name}</span>
                    {c.taxId ? (
                      <span className="meta num">
                        · {c.taxIdLabel ?? "Tax number"} {c.taxId}
                      </span>
                    ) : null}
                  </div>
                  <p className={styles.companySources}>
                    {c.sources && c.sources.length > 0 ? c.sources.map((s) => s.name).join(", ") : "Nothing is read for it yet."}
                  </p>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        <section id="learned" className={styles.section} aria-labelledby="learned-h">
          <h2 id="learned-h" className="h2">
            What I learned from them
          </h2>
          <p className={`meta ${styles.sectionLead}`}>Found in your email, documents and payments. Open one to see the list.</p>
          <div className="card list">
            {learned.map((g) => (
              <details key={g.id} id={g.id} className={`disclosure ${styles.learned}`}>
                <summary>
                  <span className={styles.learnedName}>
                    <Icon name={groupIcon[g.id] ?? "document"} size={20} />
                    {g.title}
                  </span>
                  <span className={`num ${styles.learnedCount}`}>{g.items.length}</span>
                  <Icon name="chevronDown" size={16} />
                </summary>
                <div className={styles.learnedBody}>
                  <p className="meta">{g.description}</p>
                  {g.items.length === 0 ? <p className="meta">Nothing yet.</p> : null}
                  <ul>
                    {g.items.map((item) => {
                      const s = statusLabel[item.status];
                      const more = extra(item);
                      return (
                        <li key={item.id} className={styles.known}>
                          <span className={styles.knownMain}>
                            <span className={styles.name}>{item.name}</span>
                            <span className="meta" style={{ overflowWrap: "anywhere" }}>
                              {[...new Set([item.company, item.detail].filter(Boolean))].join(" · ")}
                            </span>
                            {more ? (
                              <span className="meta" style={{ overflowWrap: "anywhere" }}>
                                {more}
                              </span>
                            ) : null}
                          </span>
                          {s ? <Status tone={s.tone} label={s.label} /> : null}
                          <button
                            className={styles.removeQuiet}
                            type="button"
                            onClick={() => void remove(item)}
                            aria-label={`Remove ${item.name}`}
                          >
                            Remove
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                  {addKind[g.id] ? (
                    <div>
                      <button className="btn btn-quiet" type="button" onClick={() => openForm(addKind[g.id] ?? "", g.id)}>
                        + Add {singular[g.id] ?? "one"}
                      </button>
                    </div>
                  ) : null}
                  {form(g.id)}
                </div>
              </details>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}
