"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useId, useState } from "react";
import detail from "@/components/detail/detail.module.css";
import {
  BackLink,
  documentHref,
  money,
  Originals,
  PageState,
  paymentHref,
  Pill,
  Result,
  Section,
} from "@/components/detail/parts";
import { send, useApi } from "@/components/detail/useApi";
import { Icon } from "@/components/Icon";
import { Bullets, Disclosure, Dot } from "@/components/ui";
import { formatDayShort, formatMonth, formatMonthShort, formatMonthYear } from "@/lib/format";
import type {
  CompanySummary,
  CostCenterDetail,
  CostCenterDocument,
  CostCenterPayment,
  CostCenterStatement,
  OpenItem,
} from "@/lib/types";

const MONTH = /^\d{4}-\d{2}$/;

function OpenItems({ items }: { items: OpenItem[] }) {
  if (items.length === 0) return null;
  return (
    <ul className={detail.openList}>
      {items.map((o) => (
        <li key={o.id}>
          <Dot tone="attention" />
          <span className={detail.openText}>
            <span>
              {o.text}
              {o.id.startsWith("nd_") ? (
                <>
                  {" "}
                  <Link href={`/needs-you#${o.id}`} className="link">
                    Answer
                  </Link>
                </>
              ) : null}
            </span>
            <Originals items={o.evidence} />
          </span>
        </li>
      ))}
    </ul>
  );
}

function PaymentRow({ p }: { p: CostCenterPayment }) {
  const sign = p.direction === "in" ? "+" : "";
  const notes = [formatDayShort(p.date)];
  if (p.split) notes.push(`its part of ${money(p.total, p.currency)}`);
  if (p.toRecharge) notes.push("the client pays it back");
  return (
    <li className={detail.rowBlock}>
      <div className={detail.rowLine}>
        <span className={detail.rowIcon}>
          <Icon name="payment" size={18} />
        </span>
        <span className={detail.rowMain}>
          <Link href={paymentHref(p.id)} className={`link ${detail.rowTitle}`}>
            {p.merchant}
          </Link>
          <span className="meta">{notes.join(" · ")}</span>
        </span>
        <span className={`num ${detail.rowAmount}`}>
          {sign}
          {money(p.amount, p.currency)}
          {p.status === "open" ? <span className={`${detail.rowSub} attention-text`}>Open</span> : null}
          {p.likely ? <span className={detail.rowSub}>Likely, not proven</span> : null}
        </span>
      </div>
      <div className={detail.indent}>
        <Disclosure summary="Why?">
          <div className="stack-2">
            <Bullets items={p.why} />
            <Originals items={p.evidence} />
          </div>
        </Disclosure>
      </div>
    </li>
  );
}

function DocumentRow({ d }: { d: CostCenterDocument }) {
  const notes = [formatDayShort(d.date), d.paid ? "paid" : "not paid yet"];
  if (d.split) notes.push(`its part of ${money(d.total, d.currency)}`);
  return (
    <li className={detail.rowBlock}>
      <div className={detail.rowLine}>
        <span className={detail.rowIcon}>
          <Icon name="document" size={18} />
        </span>
        <span className={detail.rowMain}>
          <Link href={documentHref(d.id)} className={`link ${detail.rowTitle}`}>
            {d.supplier}
          </Link>
          <span className="meta">
            {d.label.split(" · ")[0]} · {notes.join(" · ")}
          </span>
        </span>
        <span className={`num ${detail.rowAmount}`}>{money(d.amount, d.currency)}</span>
      </div>
      <div className={detail.indent}>
        <Disclosure summary="Why?">
          <div className="stack-2">
            <Bullets items={d.why} />
            <Originals items={d.evidence} />
          </div>
        </Disclosure>
      </div>
    </li>
  );
}

/** A month picker: "So far" and the company's months, as links that keep the page. */
function Months({
  months,
  current,
  hrefFor,
  allLabel,
  label,
}: {
  months: string[];
  current: string | null;
  hrefFor: (month: string | null) => string;
  allLabel?: string;
  label: string;
}) {
  return (
    <nav aria-label={label} className={detail.months}>
      <div className="segmented">
        {allLabel ? (
          <Link href={hrefFor(null)} aria-current={current === null ? "page" : undefined} scroll={false}>
            {allLabel}
          </Link>
        ) : null}
        {months.map((m) => (
          <Link key={m} href={hrefFor(m)} aria-current={m === current ? "page" : undefined} aria-label={formatMonthYear(m)} scroll={false}>
            {formatMonthShort(m)}
          </Link>
        ))}
      </div>
    </nav>
  );
}

/** One job, property, vehicle …: money out and in, its payments and documents with the proof, what is open. */
export function CostCenterView() {
  const params = useSearchParams();
  const id = params.get("id") ?? "";
  const monthParam = params.get("month");
  const month = monthParam && MONTH.test(monthParam) ? monthParam : null;
  const path = id ? `/api/cost-centers/${encodeURIComponent(id)}${month ? `?month=${month}` : ""}` : null;
  const { data, error, status, loading, reload } = useApi<CostCenterDetail>(path);
  const companies = useApi<{ companies: CompanySummary[] }>("/api/companies");

  if (!id) return <PageState loading={false} error={null} status={404} what="job" back={{ href: "/companies", label: "Your businesses" }} />;
  if (!data) {
    return (
      <PageState loading={loading} error={error} status={status} what="job" back={{ href: "/companies", label: "Your businesses" }} />
    );
  }
  const company = companies.data?.companies.find((c) => c.id === data.companyId);
  const months = company?.months ?? [];
  const base = `/companies/cost-center?id=${encodeURIComponent(id)}`;
  const when = month ? `in ${formatMonth(month)}` : "so far";

  return (
    <div className="container-narrow page">
      <BackLink href={`/companies/${encodeURIComponent(data.companyId)}#cost-centers`} label={data.companyName || "Your business"} />

      <header className={detail.head}>
        <div className={detail.headMain}>
          <h1 className={detail.title}>{data.label}</h1>
          <p className="meta">
            {[data.kind, data.companyName, data.owner ? `owner ${data.owner}` : null].filter(Boolean).join(" · ")}
          </p>
        </div>
        {!data.active ? <Pill tone="neutral">Archived</Pill> : null}
      </header>

      <div className={detail.stack}>
        <div className="stack-3">
          <p className="lead">{data.summary}</p>
          {months.length > 0 ? (
            <Months
              months={months}
              current={month}
              allLabel="So far"
              label="Period"
              hrefFor={(m) => (m ? `${base}&month=${m}` : base)}
            />
          ) : null}
          <div className={detail.tiles}>
            <div className={`card ${detail.tile}`}>
              <span className={detail.tileLabel}>Spent {when}</span>
              <span className={`num ${detail.tileValue}`}>{money(data.spent, data.currency)}</span>
            </div>
            <div className={`card ${detail.tile}`}>
              <span className={detail.tileLabel}>Received {when}</span>
              <span className={`num ${detail.tileValue}`}>{money(data.received, data.currency)}</span>
            </div>
            <div className={`card ${detail.tile}`}>
              <span className={detail.tileLabel}>Still open</span>
              <span className={`num ${detail.tileValue} ${data.openItems.length ? "attention-text" : ""}`}>
                {data.openItems.length}
              </span>
            </div>
          </div>
        </div>

        {data.openItems.length > 0 ? (
          <Section id="cc-open" title="Still open">
            <div className="card card-pad">
              <OpenItems items={data.openItems} />
            </div>
          </Section>
        ) : null}

        <Section id="cc-payments" title="Payments" aside={data.payments.length ? `${data.payments.length}` : undefined}>
          {data.payments.length ? (
            <ul className="card list">
              {data.payments.map((p) => (
                <PaymentRow key={p.id} p={p} />
              ))}
            </ul>
          ) : (
            <p className="card card-pad meta">No payments on it {when}.</p>
          )}
        </Section>

        <Section id="cc-docs" title="Documents" aside={data.documents.length ? `${data.documents.length}` : undefined}>
          {data.documents.length ? (
            <ul className="card list">
              {data.documents.map((d) => (
                <DocumentRow key={d.id} d={d} />
              ))}
            </ul>
          ) : (
            <p className="card card-pad meta">No documents on it {when}.</p>
          )}
        </Section>

        {data.recharged ? (
          <Section id="cc-recharge" title={data.recharged.clientMoney ? "Client money" : "Paid back by the client"}>
            <p className="card card-pad">{data.recharged.text}</p>
          </Section>
        ) : null}

        {data.isProperty && (companies.data || companies.error) ? (
          <OwnerStatement id={id} months={months} month={month ?? company?.currentMonth ?? null} base={base} />
        ) : null}

        <Manage data={data} onChanged={reload} />
      </div>
    </div>
  );
}

/** For a property: the owner statement of one month (money received, costs with proof, fee, what is due). */
function OwnerStatement({ id, months, month, base }: { id: string; months: string[]; month: string | null; base: string }) {
  const path = `/api/cost-centers/${encodeURIComponent(id)}/statement${month ? `?month=${month}` : ""}`;
  const { data, error } = useApi<CostCenterStatement>(path);
  return (
    <Section id="cc-statement" title="Owner statement" aside={data?.period?.label}>
      {!data ? (
        <p className="card card-pad meta" role="status">
          {error ?? "Getting the statement ready…"}
        </p>
      ) : (
        <div className={`card card-pad ${detail.form}`}>
          <div className="row-between" style={{ flexWrap: "wrap" }}>
            <h3 className="h3">{data.title}</h3>
            {data.final ? <Pill tone="good">Final</Pill> : <Pill tone="attention">Not final</Pill>}
          </div>
          {months.length > 1 ? (
            <Months months={months} current={month} label="Statement month" hrefFor={(m) => `${base}&month=${m}`} />
          ) : null}
          <StatementLines title="Money received" rows={data.moneyIn} total={data.received} currency={data.currency} empty="Nothing received." />
          <StatementLines title="Costs" rows={data.costs} total={data.spent} currency={data.currency} empty="No costs." />
          {data.managementFee ? (
            <div className={detail.usageLine}>
              <span>{data.managementFee.label}</span>
              <span className="num">−{money(data.managementFee.amount, data.currency)}</span>
            </div>
          ) : null}
          <div className={`${detail.splitTotal} ${detail.statementNet}`}>
            <span>
              {data.netDueToOwner === null
                ? "What is left"
                : data.net >= 0
                  ? `Due to ${data.owner ?? "the owner"}`
                  : `${data.owner ?? "The owner"} owes`}
            </span>
            <span className="num">{money(Math.abs(data.net), data.currency)}</span>
          </div>
          <p className="muted">{data.summary}</p>
          {data.openItems.length ? (
            <div className="stack-1">
              <h4 className="h3">Why it is not final</h4>
              <OpenItems items={data.openItems} />
            </div>
          ) : null}
        </div>
      )}
    </Section>
  );
}

function StatementLines({
  title,
  rows,
  total,
  currency,
  empty,
}: {
  title: string;
  rows: CostCenterPayment[];
  total: number;
  currency: string;
  empty: string;
}) {
  return (
    <div className="stack-1">
      <div className={detail.usageLine}>
        <span className="h3">{title}</span>
        <span className="num h3">{money(total, currency)}</span>
      </div>
      {rows.length ? (
        <ul className={detail.plainList}>
          {rows.map((r) => (
            <li key={r.id} className={detail.usageLine}>
              <span>
                <Link href={paymentHref(r.id)} className="link">
                  {r.merchant}
                </Link>{" "}
                <span className="meta">
                  {formatDayShort(r.date)}
                  {r.status === "open" ? " · open" : ""}
                </span>
              </span>
              <span className="num">{money(r.amount, r.currency)}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="meta">{empty}</p>
      )}
    </div>
  );
}

/** Rename it, change a property's owner or fee, archive it (its past costs stay on it), or bring it back. */
function Manage({ data, onChanged }: { data: CostCenterDetail; onChanged: () => void }) {
  const ids = useId();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(data.name);
  const [owner, setOwner] = useState(data.owner ?? "");
  const [fee, setFee] = useState(data.managementFee?.percent != null ? String(data.managementFee.percent) : "");
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const path = `/api/cost-centers/${encodeURIComponent(data.id)}`;

  const change = async (body: Record<string, unknown>) => {
    setBusy(true);
    setResult(null);
    const r = await send(path, body);
    setBusy(false);
    setResult({ ok: r.ok, text: r.message });
    if (r.ok) {
      setEditing(false);
      setConfirmArchive(false);
      onChanged();
    }
  };

  const save = (e: React.FormEvent) => {
    e.preventDefault();
    const body: Record<string, unknown> = {};
    if (name.trim() !== data.name) body.name = name;
    if (data.isProperty) {
      if (owner.trim() !== (data.owner ?? "")) body.owner = owner.trim();
      const before = data.managementFee?.percent != null ? String(data.managementFee.percent) : "";
      if (fee.trim() !== before) {
        body.managementFee = fee.trim()
          ? { percent: fee.trim().replace(",", "."), ...(data.managementFee?.monthly != null ? { monthly: data.managementFee.monthly } : {}) }
          : data.managementFee?.monthly != null
            ? { monthly: data.managementFee.monthly }
            : null;
      }
    }
    if (Object.keys(body).length === 0) {
      setEditing(false);
      return;
    }
    void change(body);
  };

  return (
    <Section id="cc-manage" title="Change it">
      <div className={`card card-pad ${detail.form}`}>
        {editing ? (
          <form className={detail.form} onSubmit={save} aria-label={`Change ${data.label}`}>
            <div className={detail.fields}>
              <div className={detail.field}>
                <label className="label" htmlFor={`${ids}-name`}>
                  Name
                </label>
                <input id={`${ids}-name`} className="input" value={name} maxLength={80} required onChange={(e) => setName(e.target.value)} autoFocus />
              </div>
              {data.isProperty ? (
                <>
                  <div className={detail.field}>
                    <label className="label" htmlFor={`${ids}-owner`}>
                      Owner
                    </label>
                    <input id={`${ids}-owner`} className="input" value={owner} maxLength={80} onChange={(e) => setOwner(e.target.value)} />
                  </div>
                  <div className={detail.field}>
                    <label className="label" htmlFor={`${ids}-fee`}>
                      Management fee, %
                    </label>
                    <input id={`${ids}-fee`} className="input num" inputMode="decimal" value={fee} onChange={(e) => setFee(e.target.value)} />
                  </div>
                </>
              ) : null}
            </div>
            <div className={detail.actions}>
              <button type="submit" className="btn btn-primary" disabled={busy || !name.trim()}>
                {busy ? "One moment…" : "Save"}
              </button>
              <button type="button" className="btn btn-secondary" onClick={() => setEditing(false)}>
                Cancel
              </button>
            </div>
          </form>
        ) : confirmArchive ? (
          <div className="stack-2">
            <p>
              Archive {data.label}? Its past costs stay on it. I stop putting new costs on it.
            </p>
            <div className={detail.actions}>
              <button type="button" className="btn btn-primary" disabled={busy} onClick={() => void change({ active: false })}>
                {busy ? "One moment…" : "Archive it"}
              </button>
              <button type="button" className="btn btn-secondary" onClick={() => setConfirmArchive(false)}>
                Keep it
              </button>
            </div>
          </div>
        ) : (
          <div className={detail.actions}>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setName(data.name);
                setOwner(data.owner ?? "");
                setFee(data.managementFee?.percent != null ? String(data.managementFee.percent) : "");
                setResult(null);
                setEditing(true);
              }}
            >
              {data.isProperty ? "Rename or change owner" : "Rename"}
            </button>
            {data.active ? (
              <button type="button" className="btn btn-quiet" onClick={() => setConfirmArchive(true)}>
                Archive
              </button>
            ) : (
              <button type="button" className="btn btn-secondary" disabled={busy} onClick={() => void change({ active: true })}>
                Bring it back
              </button>
            )}
          </div>
        )}
        <Result result={result} />
      </div>
    </Section>
  );
}
