"use client";

import { useId, useRef, useState } from "react";
import detail from "@/components/detail/detail.module.css";
import { BackLink, money, OriginalIds, Pill, Result, Section } from "@/components/detail/parts";
import { send, useApi } from "@/components/detail/useApi";
import { Icon } from "@/components/Icon";
import { Loading } from "@/components/live/Loading";
import { answerNeedsYou, filePayload } from "@/lib/api";
import { formatDayShort } from "@/lib/format";
import { markAnswered } from "@/lib/resolved-store";
import type { CompanySummary, Employee, ExpenseClaim, Tone } from "@/lib/types";

type Outcome = { ok: boolean; text: string } | null;

const CLAIM_TONE: Record<string, Tone> = { waiting: "attention", approved: "neutral", paid: "good", declined: "neutral" };

function firstName(name: string): string {
  return name.trim().split(/\s+/)[0] || name;
}

/** "4817, 5521" → ["4817", "5521"]; anything else is the engine's to refuse in plain words. */
function cardsFrom(text: string): string[] {
  return text
    .split(/[,\s]+/)
    .map((c) => c.trim())
    .filter(Boolean);
}

/**
 * People who pay for the business: company cards (I ask them, not you, for the receipts) and their own
 * money (expense claims: one tap to pay them back or not, then the transfer closes it).
 */
export function PeopleView() {
  const people = useApi<{ employees: Employee[] }>("/api/employees");
  const claims = useApi<{ claims: ExpenseClaim[] }>("/api/expense-claims");
  const companies = useApi<{ companies: CompanySummary[] }>("/api/companies");

  if (people.loading || claims.loading) return <Loading />;
  const list = companies.data?.companies ?? [];
  const reloadAll = () => {
    people.reload();
    claims.reload();
  };

  return (
    <div className="container-narrow page">
      <BackLink href="/settings" label="Settings" />
      <header className="page-head">
        <h1 className="h1">People and expenses</h1>
        <p className="lead">
          The people who pay for the business. When a card payment misses its receipt, I ask the person who holds the card, not you.
        </p>
      </header>

      {people.error && !people.data ? (
        <p className="card card-pad muted" role="status">
          {people.error}
        </p>
      ) : (
        <div className={detail.stack}>
          <People employees={people.data?.employees ?? []} companies={list} onChanged={reloadAll} />
          <Claims
            error={claims.data ? null : claims.error}
            claims={claims.data?.claims ?? []}
            employees={people.data?.employees ?? []}
            companies={list}
            onChanged={reloadAll}
          />
        </div>
      )}
    </div>
  );
}

/* ---------- People and their cards ---------- */

function People({ employees, companies, onChanged }: { employees: Employee[]; companies: CompanySummary[]; onChanged: () => void }) {
  const [adding, setAdding] = useState(false);
  return (
    <Section
      id="people"
      title="People"
      action={
        <button type="button" className="btn btn-quiet" aria-expanded={adding} aria-controls="person-add" onClick={() => setAdding((v) => !v)}>
          <Icon name="plus" size={18} />
          Add someone
        </button>
      }
    >
      {adding ? (
        <PersonForm
          formId="person-add"
          companies={companies}
          onClose={() => setAdding(false)}
          onSaved={() => {
            onChanged();
          }}
        />
      ) : null}
      {employees.length ? (
        <ul className="card list">
          {employees.map((e) => (
            <PersonRow key={e.id} employee={e} companies={companies} onChanged={onChanged} />
          ))}
        </ul>
      ) : !adding ? (
        <p className="card card-pad muted">
          Nobody yet. Add the people who hold a company card or pay small things with their own money.
        </p>
      ) : null}
    </Section>
  );
}

function PersonRow({ employee: e, companies, onChanged }: { employee: Employee; companies: CompanySummary[]; onChanged: () => void }) {
  const [editing, setEditing] = useState(false);
  const facts = [e.email || "No email yet", e.companyName, ...e.cards.map((c) => c.label)].filter(Boolean);
  const status: string[] = [];
  if (e.receiptsMissing) {
    status.push(
      `${e.receiptsMissing} ${e.receiptsMissing === 1 ? "receipt" : "receipts"} missing${e.receiptsAsked ? `, ${e.receiptsAsked} asked for` : ""}`,
    );
  }
  if (e.claimsWaiting) status.push(`${e.claimsWaiting} waiting for your OK`);
  if (e.toPayBack) status.push(`${money(e.toPayBack)} to pay back`);
  return (
    <li className={detail.rowBlock}>
      <div className={detail.rowLine}>
        <span className={detail.rowIcon}>
          <Icon name="user" size={18} />
        </span>
        <span className={detail.rowMain}>
          <span className={detail.rowTitle}>{e.name}</span>
          <span className="meta">{facts.join(" · ")}</span>
          {status.length ? <span className="meta attention-text">{status.join(" · ")}</span> : null}
        </span>
        <button
          type="button"
          className="btn btn-quiet"
          aria-expanded={editing}
          aria-label={`Change ${e.name}`}
          onClick={() => setEditing((v) => !v)}
        >
          Change
        </button>
      </div>
      {editing ? (
        <PersonForm
          formId={`person-${e.id}`}
          employee={e}
          companies={companies}
          onClose={() => setEditing(false)}
          onSaved={() => {
            onChanged();
          }}
        />
      ) : null}
    </li>
  );
}

function PersonForm({
  formId,
  employee,
  companies,
  onClose,
  onSaved,
}: {
  formId: string;
  employee?: Employee;
  companies: CompanySummary[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const ids = useId();
  const [name, setName] = useState(employee?.name ?? "");
  const [email, setEmail] = useState(employee?.email ?? "");
  const [phone, setPhone] = useState(employee?.phone ?? "");
  const [company, setCompany] = useState(employee?.companyId ?? (companies.length === 1 ? (companies[0]?.id ?? "") : ""));
  const [cards, setCards] = useState((employee?.cards ?? []).map((c) => c.last4).join(", "));
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Outcome>(null);

  const submit = async (ev: React.FormEvent) => {
    ev.preventDefault();
    setBusy(true);
    setResult(null);
    const body: Record<string, unknown> = { name, email: email.trim(), cards: cardsFrom(cards) };
    if (phone.trim() || employee?.phone) body.phone = phone.trim();
    if (company) body.companyId = company;
    const r = await send(employee ? `/api/employees/${encodeURIComponent(employee.id)}` : "/api/employees", body);
    setBusy(false);
    setResult({ ok: r.ok, text: r.message });
    if (r.ok) {
      if (!employee) {
        setName("");
        setEmail("");
        setPhone("");
        setCards("");
      }
      onSaved();
    }
  };

  return (
    <form id={formId} className={`card card-pad ${detail.form}`} onSubmit={submit} aria-label={employee ? `Change ${employee.name}` : "Add someone"}>
      <div className={detail.fields}>
        <div className={detail.field}>
          <label className="label" htmlFor={`${ids}-name`}>
            Name
          </label>
          <input id={`${ids}-name`} className="input" value={name} required maxLength={120} autoComplete="off" onChange={(e) => setName(e.target.value)} />
        </div>
        <div className={detail.field}>
          <label className="label" htmlFor={`${ids}-email`}>
            Email
          </label>
          <input
            id={`${ids}-email`}
            className="input"
            type="email"
            value={email}
            autoComplete="off"
            aria-describedby={`${ids}-email-hint`}
            onChange={(e) => setEmail(e.target.value)}
          />
          <span id={`${ids}-email-hint`} className={detail.hint}>
            Where I ask them for receipts.
          </span>
        </div>
        <div className={detail.field}>
          <label className="label" htmlFor={`${ids}-cards`}>
            Company cards
          </label>
          <input
            id={`${ids}-cards`}
            className="input num"
            inputMode="numeric"
            value={cards}
            placeholder="4817"
            autoComplete="off"
            aria-describedby={`${ids}-cards-hint`}
            onChange={(e) => setCards(e.target.value)}
          />
          <span id={`${ids}-cards-hint`} className={detail.hint}>
            The last 4 digits of each card they hold, separated by commas.
          </span>
        </div>
        {companies.length > 1 ? (
          <div className={detail.field}>
            <label className="label" htmlFor={`${ids}-company`}>
              Works for
            </label>
            <select id={`${ids}-company`} className="input" value={company} onChange={(e) => setCompany(e.target.value)}>
              <option value="">Not one company</option>
              {companies.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        {employee ? (
          <div className={detail.field}>
            <label className="label" htmlFor={`${ids}-phone`}>
              Phone <span className="meta">(optional)</span>
            </label>
            <input id={`${ids}-phone`} className="input" type="tel" value={phone} autoComplete="off" onChange={(e) => setPhone(e.target.value)} />
          </div>
        ) : null}
      </div>
      <div className={detail.actions}>
        <button type="submit" className="btn btn-primary" disabled={busy || !name.trim()}>
          {busy ? "One moment…" : employee ? "Save" : "Add"}
        </button>
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          Close
        </button>
      </div>
      <Result result={result} />
    </form>
  );
}

/* ---------- Expense claims ---------- */

function Claims({
  error,
  claims,
  employees,
  companies,
  onChanged,
}: {
  error: string | null;
  claims: ExpenseClaim[];
  employees: Employee[];
  companies: CompanySummary[];
  onChanged: () => void;
}) {
  const [sending, setSending] = useState(false);
  const waiting = claims.filter((c) => c.status === "waiting").length;
  return (
    <Section
      id="claims"
      title="Expense claims"
      action={
        employees.length ? (
          <button type="button" className="btn btn-quiet" aria-expanded={sending} aria-controls="claim-send" onClick={() => setSending((v) => !v)}>
            <Icon name="upload" size={18} />
            Send a receipt for someone
          </button>
        ) : undefined
      }
    >
      <p className="muted">
        {waiting
          ? `${waiting === 1 ? "One receipt waits" : `${waiting} receipts wait`} for your OK to pay it back.`
          : "Receipts people paid with their own money. I close each one when the transfer paying them back shows in your bank."}
      </p>
      {sending ? <SendReceipt employees={employees} companies={companies} onClose={() => setSending(false)} onSent={onChanged} /> : null}
      {claims.length ? (
        <ul className="card list">
          {claims.map((c) => (
            <ClaimRow key={c.id} claim={c} onChanged={onChanged} />
          ))}
        </ul>
      ) : (
        <p className="card card-pad meta" role={error ? "status" : undefined}>
          {error ?? "No expense claims yet."}
        </p>
      )}
    </Section>
  );
}

function ClaimRow({ claim: c, onChanged }: { claim: ExpenseClaim; onChanged: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<Outcome>(null);
  const first = firstName(c.employee);
  const answer = async (option: "approve" | "decline") => {
    if (!c.needsId) return;
    setBusy(option);
    setResult(null);
    const r = await answerNeedsYou(c.needsId, option, false);
    setBusy(null);
    setResult({ ok: r.ok, text: r.message ?? (r.ok ? "Done." : "I couldn’t save that. Try again.") });
    if (r.ok) {
      markAnswered(c.needsId);
      onChanged();
    }
  };
  return (
    <li className={detail.rowBlock} aria-label={`${c.employee}, ${c.merchant}`}>
      <div className={detail.rowLine}>
        <span className={detail.rowIcon}>
          <Icon name="document" size={18} />
        </span>
        <span className={detail.rowMain}>
          <span className={detail.rowTitle}>
            {c.employee} · {c.merchant}
          </span>
          <span className="meta">
            {[formatDayShort(c.date), c.companyName].filter(Boolean).join(" · ")}
          </span>
        </span>
        <span className={`num ${detail.rowAmount}`}>{money(c.amount, c.currency)}</span>
      </div>
      <div className={`${detail.indent} stack-2`}>
        <div className={detail.pills}>
          <Pill tone={CLAIM_TONE[c.status] ?? "neutral"}>{c.statusLabel}</Pill>
        </div>
        {c.note ? <p className="muted">{c.note}</p> : null}
        <OriginalIds ids={c.evidenceIds} label={() => `The receipt from ${c.merchant}`} />
        {c.status === "waiting" && c.needsId ? (
          <div className={detail.actions}>
            <button type="button" className="btn btn-primary" disabled={busy !== null} onClick={() => void answer("approve")}>
              {busy === "approve" ? "One moment…" : `Yes, pay ${first} back`}
            </button>
            <button type="button" className="btn btn-secondary" disabled={busy !== null} onClick={() => void answer("decline")}>
              {busy === "decline" ? "One moment…" : "No, don’t pay it back"}
            </button>
          </div>
        ) : null}
        <Result result={result} />
      </div>
    </li>
  );
}

function SendReceipt({
  employees,
  companies,
  onClose,
  onSent,
}: {
  employees: Employee[];
  companies: CompanySummary[];
  onClose: () => void;
  onSent: () => void;
}) {
  const ids = useId();
  const fileRef = useRef<HTMLInputElement>(null);
  const [who, setWho] = useState(employees[0]?.id ?? "");
  const [company, setCompany] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Outcome>(null);

  const submit = async (ev: React.FormEvent) => {
    ev.preventDefault();
    const file = fileRef.current?.files?.[0];
    if (!file) {
      setResult({ ok: false, text: "Choose the photo or PDF of the receipt." });
      return;
    }
    setBusy(true);
    setResult(null);
    const body: Record<string, unknown> = { employeeId: who, ...(await filePayload(file)) };
    if (company) body.companyId = company;
    const r = await send("/api/expense-claims", body);
    setBusy(false);
    setResult({ ok: r.ok, text: r.message });
    if (r.ok) {
      if (fileRef.current) fileRef.current.value = "";
      onSent();
    }
  };

  return (
    <form id="claim-send" className={`card card-pad ${detail.form}`} onSubmit={submit} aria-label="Send a receipt for someone">
      <div className={detail.fields}>
        <div className={detail.field}>
          <label className="label" htmlFor={`${ids}-who`}>
            Who paid
          </label>
          <select id={`${ids}-who`} className="input" value={who} onChange={(e) => setWho(e.target.value)}>
            {employees.map((e) => (
              <option key={e.id} value={e.id}>
                {e.name}
              </option>
            ))}
          </select>
        </div>
        {companies.length > 1 ? (
          <div className={detail.field}>
            <label className="label" htmlFor={`${ids}-company`}>
              For
            </label>
            <select id={`${ids}-company`} className="input" value={company} onChange={(e) => setCompany(e.target.value)}>
              <option value="">Their company</option>
              {companies.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
        ) : null}
        <div className={detail.field}>
          <label className="label" htmlFor={`${ids}-file`}>
            The receipt
          </label>
          <input id={`${ids}-file`} ref={fileRef} className="input" type="file" accept="image/*,application/pdf,text/plain" />
        </div>
      </div>
      <div className={detail.actions}>
        <button type="submit" className="btn btn-primary" disabled={busy || !who}>
          {busy ? "One moment…" : "Send"}
        </button>
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          Close
        </button>
      </div>
      <Result result={result} />
    </form>
  );
}
