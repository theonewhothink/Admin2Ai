"use client";

import Link from "next/link";
import { useId, useState } from "react";
import detail from "@/components/detail/detail.module.css";
import { Result } from "@/components/detail/parts";
import { send, useApi } from "@/components/detail/useApi";
import { Icon } from "@/components/Icon";
import { Disclosure, Dot } from "@/components/ui";
import { liveData } from "@/lib/api";
import { formatMoney } from "@/lib/format";
import type { CostCenterRow, CostCentersData } from "@/lib/types";

/** The kinds a business uses, in its own words; the engine accepts any one or two words. */
const KINDS = ["Job", "Property", "Apartment", "Vehicle", "Outlet", "Event", "Course", "Client"];
/** Kinds the engine treats as a property (an owner statement, a management fee). */
const PROPERTY = /^(property|apartment|flat|unit|house|building|villa|room|studio|im[oó]vel|apartamento|fra[cç][aã]o|moradia|pr[eé]dio|casa)$/i;

export function costCenterHref(id: string): string {
  return `/companies/cost-center?id=${encodeURIComponent(id)}`;
}

function article(word: string): string {
  return /^[aeiou]/i.test(word) ? "an" : "a";
}

function Row({ c }: { c: CostCenterRow }) {
  const parts = [`${formatMoney(c.spent, c.currency)} spent`];
  if (c.received) parts.push(`${formatMoney(c.received, c.currency)} received`);
  return (
    <li>
      <Link href={costCenterHref(c.id)} className={detail.row}>
        <span className={detail.rowMain}>
          <span className={detail.rowTitle}>{c.label}</span>
          <span className="meta num">
            {parts.join(" · ")}
            {c.owner ? ` · owner ${c.owner}` : ""}
          </span>
        </span>
        {c.openItems > 0 ? (
          <span className="status status-attention">
            <Dot tone="attention" />
            {c.openItems} open
          </span>
        ) : c.payments + c.documents > 0 ? (
          <span className="status status-neutral">Nothing open</span>
        ) : null}
        <Icon name="chevronRight" size={18} className={detail.chevron} />
      </Link>
    </li>
  );
}

/**
 * A company's jobs, properties, vehicles, outlets, events, courses or clients (cost centers), in the
 * company's own word: what each cost and brought in, and what is still open. Add one here.
 */
export function CostCenters({ companyId }: { companyId: string }) {
  const path = liveData ? `/api/companies/${encodeURIComponent(companyId)}/cost-centers` : null;
  const { data, reload } = useApi<CostCentersData>(path);
  const [adding, setAdding] = useState(false);
  if (!data) return null;

  const kind = data.kind;
  const active = data.costCenters.filter((c) => c.active);
  const archived = data.costCenters.filter((c) => !c.active);
  const waiting = data.notDecided.needsYouIds;

  return (
    <section aria-labelledby="cc-h" className={detail.section} id="cost-centers">
      <div className="section-head">
        <h2 id="cc-h" className="h2">
          {data.kindPlural}
        </h2>
        <button
          type="button"
          className="btn btn-quiet"
          onClick={() => setAdding((v) => !v)}
          aria-expanded={adding}
          aria-controls="cc-add"
        >
          <Icon name="plus" size={18} />
          Add {article(kind)} {kind.toLowerCase()}
        </button>
      </div>
      {waiting.length > 0 ? (
        <div className="notice notice-attention">
          <Dot tone="attention" />
          <p style={{ flex: 1 }}>
            {data.headline}{" "}
            <Link href={`/needs-you#${waiting[0]}`} className="link">
              Answer
            </Link>
          </p>
        </div>
      ) : (
        <p className="muted">{data.headline}</p>
      )}

      {adding ? (
        <AddCostCenter
          companyId={companyId}
          kind={kind}
          onClose={() => setAdding(false)}
          onAdded={() => {
            reload();
          }}
        />
      ) : null}

      {active.length > 0 ? (
        <ul className="card list">
          {active.map((c) => (
            <Row key={c.id} c={c} />
          ))}
          {data.general.payments > 0 ? (
            <li className={detail.row}>
              <span className={detail.rowMain}>
                <span className={detail.rowTitle}>General costs</span>
                <span className="meta num">
                  {formatMoney(data.general.spent)} spent · not for one {kind.toLowerCase()}
                </span>
              </span>
            </li>
          ) : null}
        </ul>
      ) : null}

      {archived.length > 0 ? (
        <Disclosure summary={`Archived (${archived.length})`}>
          <ul className="card list">
            {archived.map((c) => (
              <Row key={c.id} c={c} />
            ))}
          </ul>
        </Disclosure>
      ) : null}
    </section>
  );
}

function AddCostCenter({
  companyId,
  kind,
  onClose,
  onAdded,
}: {
  companyId: string;
  kind: string;
  onClose: () => void;
  onAdded: () => void;
}) {
  const ids = useId();
  const [name, setName] = useState("");
  const [type, setType] = useState(kind);
  const [owner, setOwner] = useState("");
  const [fee, setFee] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const property = PROPERTY.test(type.trim());

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setResult(null);
    const body: Record<string, unknown> = { name, kind: type.trim() || kind };
    if (property && owner.trim()) body.owner = owner.trim();
    if (property && fee.trim()) body.managementFee = { percent: fee.trim().replace(",", ".") };
    const r = await send("/api/companies/" + encodeURIComponent(companyId) + "/cost-centers", body);
    setBusy(false);
    setResult({ ok: r.ok, text: r.message });
    if (r.ok) {
      setName("");
      setOwner("");
      setFee("");
      onAdded();
    }
  };

  return (
    <form id="cc-add" className={`card card-pad ${detail.form}`} onSubmit={submit} aria-label={`Add ${article(kind)} ${kind.toLowerCase()}`}>
      <div className={detail.fields}>
        <div className={detail.field}>
          <label className="label" htmlFor={`${ids}-name`}>
            Name
          </label>
          <input
            id={`${ids}-name`}
            className="input"
            value={name}
            autoFocus
            required
            maxLength={80}
            placeholder="Rua das Flores 12"
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className={detail.field}>
          <label className="label" htmlFor={`${ids}-kind`}>
            Kind
          </label>
          <input
            id={`${ids}-kind`}
            className="input"
            value={type}
            list={`${ids}-kinds`}
            maxLength={30}
            onChange={(e) => setType(e.target.value)}
          />
          <datalist id={`${ids}-kinds`}>
            {KINDS.map((k) => (
              <option key={k} value={k} />
            ))}
          </datalist>
        </div>
        {property ? (
          <>
            <div className={detail.field}>
              <label className="label" htmlFor={`${ids}-owner`}>
                Owner <span className="meta">(optional)</span>
              </label>
              <input id={`${ids}-owner`} className="input" value={owner} maxLength={80} onChange={(e) => setOwner(e.target.value)} />
            </div>
            <div className={detail.field}>
              <label className="label" htmlFor={`${ids}-fee`}>
                Management fee, % <span className="meta">(optional)</span>
              </label>
              <input
                id={`${ids}-fee`}
                className="input num"
                inputMode="decimal"
                value={fee}
                placeholder="10"
                onChange={(e) => setFee(e.target.value)}
              />
            </div>
          </>
        ) : null}
      </div>
      <p className={detail.hint}>I put each cost on it when something on the payment or the invoice points to it, and ask you when nothing does.</p>
      <div className={detail.actions}>
        <button type="submit" className="btn btn-primary" disabled={busy || !name.trim()}>
          {busy ? "One moment…" : "Add"}
        </button>
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          Close
        </button>
      </div>
      <Result result={result} />
    </form>
  );
}
