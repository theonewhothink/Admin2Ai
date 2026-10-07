"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useState } from "react";
import detail from "@/components/detail/detail.module.css";
import {
  BackLink,
  ChainList,
  day,
  documentHref,
  Facts,
  ImportChainView,
  money,
  Originals,
  PageState,
  paymentHref,
  Pill,
  Result,
  Section,
  stageTone,
  Timeline,
} from "@/components/detail/parts";
import { send, useApi } from "@/components/detail/useApi";
import { Icon } from "@/components/Icon";
import { CheckList, Dot } from "@/components/ui";
import { download } from "@/lib/api";
import type { DocumentDetail as Doc, StatementLine, SupplierStatement } from "@/lib/types";

const LINE_TONE: Record<string, "good" | "attention" | "risk" | "neutral"> = {
  matched: "good",
  missing: "attention",
  differs: "attention",
  not_found: "attention",
};

function Lines({ lines }: { lines: StatementLine[] }) {
  return (
    <ul className={detail.openList}>
      {lines.map((l) => (
        <li key={l.row}>
          <Dot tone={LINE_TONE[l.status] ?? "neutral"} />
          <span className={detail.openText}>
            <span>
              {l.text}
              {l.document ? (
                <>
                  {" "}
                  <Link href={documentHref(l.document.id)} className="link">
                    Open
                  </Link>
                </>
              ) : null}
            </span>
          </span>
        </li>
      ))}
    </ul>
  );
}

/** A supplier's account statement, line by line against the business's own records. Never booked. */
function StatementCheck({ s }: { s: SupplierStatement }) {
  const lines = s.lines ?? [];
  return (
    <Section id="doc-statement" title="Checked against your records" aside={s.complete ? "Everything matches" : undefined}>
      <div className={`card card-pad ${detail.form}`}>
        <p>{s.summary}</p>
        {lines.length ? <Lines lines={lines} /> : null}
        {s.notOnStatement.length ? (
          <div className="stack-1">
            <h3 className="h3">Not on their statement</h3>
            {s.notOnStatementText ? <p className="muted">{s.notOnStatementText}</p> : null}
            <ul className={detail.plainList}>
              {s.notOnStatement.map((d) => (
                <li key={d.id}>
                  <Link href={documentHref(d.id)} className="link">
                    {d.label}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {s.balance ? <p className={s.balance.agrees ? "" : "attention-text"}>{s.balance.text}</p> : null}
        {s.request ? (
          <p className="muted">
            {s.request.status === "sent"
              ? `I asked ${s.request.to ?? "them"} for what is missing.`
              : s.request.status === "waiting"
                ? "My request for what is missing is written and waits to be sent."
                : s.request.text}
          </p>
        ) : null}
        {s.needsYouId ? (
          <p>
            <Link href={`/needs-you#${s.needsYouId}`} className="link">
              Tell me which amount is right
            </Link>
          </p>
        ) : null}
        <p className="meta">{s.note}</p>
      </div>
    </Section>
  );
}

/** One document: what it is, its proof, the payments it proves, credit notes and refunds, its history. */
export function DocumentDetailView() {
  const id = useSearchParams().get("id") ?? "";
  const path = id ? `/api/documents/${encodeURIComponent(id)}` : null;
  const { data, error, status, loading, reload } = useApi<Doc>(path);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const back = { href: "/documents", label: "Documents" };

  if (!id) return <PageState loading={false} error={null} status={404} what="document" back={back} />;
  if (!data) return <PageState loading={loading} error={error} status={status} what="document" back={back} />;

  const sensitive = Boolean(data.sensitive);
  const toggleSensitive = async () => {
    setBusy(true);
    setResult(null);
    const r = await send(`/api/documents/${encodeURIComponent(data.id)}/sensitive`, { sensitive: !sensitive });
    setBusy(false);
    setResult({ ok: r.ok, text: r.message });
    if (r.ok) reload();
  };

  return (
    <div className="container-narrow page">
      <BackLink {...back} />

      <header className={detail.head}>
        <div className={detail.headMain}>
          <h1 className={detail.title}>{data.supplier}</h1>
          <p className="meta">
            {[data.label.split(" · ")[0], day(data.date), data.companyName || "No company yet"].filter(Boolean).join(" · ")}
          </p>
          <div className={detail.pills}>
            <Pill tone={stageTone(data.stage)}>{data.statusLabel || data.stage}</Pill>
            {sensitive ? (
              <Pill tone="neutral">
                <Icon name="lock" size={14} />
                Sensitive
              </Pill>
            ) : null}
          </div>
        </div>
        {data.amount !== null ? <div className={`num ${detail.bigAmount}`}>{money(data.amount, data.currency)}</div> : null}
      </header>

      <div className={detail.stack}>
        {data.waiting ? (
          <div className="notice notice-attention">
            <Dot tone="attention" />
            <p>{data.waiting}</p>
          </div>
        ) : null}
        {data.dueLine ? (
          <div className="notice">
            <Icon name="clock" size={18} />
            <p>{data.dueLine}</p>
          </div>
        ) : null}

        <div className="card card-pad stack-3">
          <Facts
            items={[
              { label: "Kind", value: data.type ? data.type.charAt(0).toUpperCase() + data.type.slice(1) : "" },
              { label: "Number", value: data.number },
              { label: "Company", value: data.companyName || "Not decided yet" },
              { label: "Date", value: day(data.date) },
              { label: "Due", value: day(data.due ?? null) },
              { label: "Amount", value: money(data.amount, data.currency) },
            ]}
          />
          <div className="stack-1">
            <h2 className="h3">Proof</h2>
            <Originals items={data.evidence} label="The original" />
            <div className={detail.actions}>
              <button
                type="button"
                className="btn btn-quiet"
                onClick={async () => setNote(await download(`/api/documents/${encodeURIComponent(data.id)}/file`))}
              >
                <Icon name="download" size={18} />
                Download the original
              </button>
            </div>
            {note ? (
              <p className="meta" role="alert">
                {note}
              </p>
            ) : null}
          </div>
          {data.why.length ? (
            <div className="stack-1">
              <h2 className="h3">Why</h2>
              <CheckList items={data.why} />
            </div>
          ) : null}
        </div>

        {data.payments.length || data.supportsPayments.length ? (
          <Section id="doc-payments" title={data.payments.length === 1 ? "The payment" : "Payments"}>
            <ul className="card list">
              {data.payments.map((p) => (
                <li key={p.transactionId}>
                  <Link href={paymentHref(p.transactionId)} className={detail.row}>
                    <span className={detail.rowIcon}>
                      <Icon name="payment" size={18} />
                    </span>
                    <span className={detail.rowMain}>
                      <span className={detail.rowTitle}>{p.label}</span>
                    </span>
                    <Icon name="chevronRight" size={18} className={detail.chevron} />
                  </Link>
                </li>
              ))}
              {data.supportsPayments.map((p) => (
                <li key={p.id} className={detail.row}>
                  <span className={detail.rowIcon}>
                    <Icon name="payment" size={18} />
                  </span>
                  <span className={detail.rowMain}>
                    <span className={detail.rowTitle}>{p.label}</span>
                    <span className="meta">Supports this payment, not an invoice</span>
                  </span>
                </li>
              ))}
            </ul>
          </Section>
        ) : null}

        {data.received ? (
          <Section id="doc-received" title="Paid in parts">
            <div className="card card-pad stack-2">
              <p>{data.received.text}</p>
              <Facts
                items={[
                  { label: "Received so far", value: money(data.received.received, data.currency) },
                  { label: "Still to come", value: money(data.received.stillToCome, data.currency) },
                ]}
              />
            </div>
          </Section>
        ) : null}

        {data.heldBack ? (
          <Section id="doc-held" title="Held back">
            <p className="card card-pad">{data.heldBack.text}</p>
          </Section>
        ) : null}

        {data.parts?.length ? (
          <Section id="doc-parts" title="Deposit and parts">
            <ChainList steps={data.parts} current={data.id} label="Deposit and parts" />
          </Section>
        ) : null}

        {data.corrects || data.creditNotes.length ? (
          <Section id="doc-credit" title="Corrections">
            <ul className={`card card-pad ${detail.plainList}`}>
              {data.corrects ? (
                <li>
                  This corrects{" "}
                  <Link href={documentHref(data.corrects.id)} className="link">
                    {data.corrects.label}
                  </Link>
                  .
                </li>
              ) : null}
              {data.creditNotes.map((n) => (
                <li key={n.id}>
                  Corrected by{" "}
                  <Link href={documentHref(n.id)} className="link">
                    {n.label}
                  </Link>
                  .
                </li>
              ))}
            </ul>
          </Section>
        ) : null}

        {data.chain.length ? (
          <Section id="doc-chain" title="Invoice, payment, credit note and refund">
            <ChainList steps={data.chain} current={data.id} label="Refund chain" />
          </Section>
        ) : null}

        {data.importChain ? (
          <Section id="doc-import" title={data.importChain.name || "Import"}>
            <ImportChainView chain={data.importChain} />
          </Section>
        ) : null}

        {data.statement ? <StatementCheck s={data.statement} /> : null}

        {data.history.length ? (
          <Section id="doc-history" title="What happened">
            <div className="card">
              <Timeline steps={data.history} />
            </div>
          </Section>
        ) : null}

        <Section id="doc-privacy" title="Who can see it">
          <div className={`card card-pad ${detail.form}`}>
            <p className="muted">
              {sensitive
                ? `${data.sensitiveReason ? `${data.sensitiveReason} ` : ""}Only you and the company’s accountant can see it, and I note every time its original is opened.`
                : "Everyone with access to this business can see it. Mark it sensitive for payslips, medical or personal papers."}
            </p>
            <div className={detail.actions}>
              <button type="button" className="btn btn-secondary" disabled={busy} onClick={toggleSensitive}>
                <Icon name="lock" size={18} />
                {busy ? "One moment…" : sensitive ? "It is not sensitive" : "Mark as sensitive"}
              </button>
            </div>
            <Result result={result} />
          </div>
        </Section>
      </div>
    </div>
  );
}
