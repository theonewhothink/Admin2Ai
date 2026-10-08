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
  OriginalIds,
  PageState,
  Pill,
  Result,
  Section,
  stageTone,
  Timeline,
} from "@/components/detail/parts";
import { send, useApi } from "@/components/detail/useApi";
import { Icon } from "@/components/Icon";
import { Bullets, CheckList, Dot } from "@/components/ui";
import type { TransactionDetail } from "@/lib/types";

/** One payment: what it needs, its documents and why they match, what happens next, its history. */
export function PaymentDetailView() {
  const id = useSearchParams().get("id") ?? "";
  const path = id ? `/api/transactions/${encodeURIComponent(id)}` : null;
  const { data, error, status, loading, reload } = useApi<TransactionDetail>(path);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const back = data?.companyId
    ? { href: `/companies/${encodeURIComponent(data.companyId)}`, label: data.companyName || "Your business" }
    : { href: "/companies", label: "Your businesses" };

  if (!id) return <PageState loading={false} error={null} status={404} what="payment" back={back} />;
  if (!data) return <PageState loading={loading} error={error} status={status} what="payment" back={back} />;

  const teach = async (need: "none" | "invoice") => {
    setBusy(need);
    setResult(null);
    const r = await send(`/api/transactions/${encodeURIComponent(data.id)}/evidence`, { need });
    setBusy(null);
    setResult({ ok: r.ok, text: r.message });
    if (r.ok) reload();
  };

  const incoming = data.direction === "in";
  return (
    <div className="container-narrow page">
      <BackLink {...back} />

      <header className={detail.head}>
        <div className={detail.headMain}>
          <h1 className={detail.title}>{data.counterparty}</h1>
          <p className="meta">
            {[incoming ? "Money in" : "Money out", day(data.date), data.companyName].filter(Boolean).join(" · ")}
          </p>
          <div className={detail.pills}>
            <Pill tone={stageTone(data.status)}>{data.statusLabel || data.status}</Pill>
          </div>
        </div>
        <div className={`num ${detail.bigAmount}`}>
          {incoming ? "+" : ""}
          {money(data.amount, data.currency)}
        </div>
      </header>

      <div className={detail.stack}>
        {data.disputed ? (
          <div className="notice notice-attention">
            <Dot tone="attention" />
            <p>{data.disputed.text}</p>
          </div>
        ) : data.nextStep ? (
          <div className="notice">
            <Icon name="clock" size={18} />
            <p>{data.nextStep}</p>
          </div>
        ) : null}

        {data.evidenceChoices?.length ? (
          <Section id="pay-teach" title="Does it have an invoice?">
            <div className={`card card-pad ${detail.form}`}>
              <p className="muted">One answer settles it for every payment to {data.counterparty}, now and later.</p>
              <div className={detail.actions}>
                {data.evidenceChoices.map((c) => (
                  <button
                    key={c.need}
                    type="button"
                    className="btn btn-secondary"
                    disabled={busy !== null}
                    onClick={() => void teach(c.need)}
                  >
                    {busy === c.need ? "One moment…" : c.label}
                  </button>
                ))}
              </div>
              <Result result={result} />
            </div>
          </Section>
        ) : result ? (
          <div className="card card-pad">
            <Result result={result} />
          </div>
        ) : null}

        <div className="card card-pad stack-3">
          <Facts
            items={[
              { label: "What it needs", value: data.expects },
              { label: "Company", value: data.companyName || "Not decided yet" },
              { label: "Date", value: day(data.date) },
              { label: "Amount", value: money(data.amount, data.currency) },
            ]}
          />
          <div className="stack-1">
            <h2 className="h3">Proof</h2>
            <OriginalIds ids={data.evidenceIds} label={() => `The bank line · ${day(data.date)} · ${money(data.amount, data.currency)}`} />
          </div>
          {data.notes?.length ? <Bullets items={data.notes} /> : null}
        </div>

        <Section id="pay-docs" title={data.documents.length === 1 ? "The document" : "Documents"}>
          {data.documents.length ? (
            <div className="card">
              <ul className="list">
                {data.documents.map((d) => (
                  <li key={d.id}>
                    <Link href={documentHref(d.id)} className={detail.row}>
                      <span className={detail.rowIcon}>
                        <Icon name="document" size={18} />
                      </span>
                      <span className={detail.rowMain}>
                        <span className={detail.rowTitle}>{d.label}</span>
                      </span>
                      <Icon name="chevronRight" size={18} className={detail.chevron} />
                    </Link>
                  </li>
                ))}
              </ul>
              {data.why.length ? (
                <div className={`card-pad stack-1 ${detail.whyBlock}`}>
                  <h3 className="h3">{data.headline || "Why they match"}</h3>
                  <CheckList items={data.why} />
                </div>
              ) : null}
            </div>
          ) : (
            <p className="card card-pad meta">No document is matched to it yet.</p>
          )}
        </Section>

        {data.deposit ? (
          <Section id="pay-deposit" title="Deposit">
            <p className="card card-pad">{data.deposit.text}</p>
          </Section>
        ) : null}

        {data.parts?.length ? (
          <Section id="pay-parts" title="Deposit and parts">
            <ChainList steps={data.parts} current={data.id} label="Deposit and parts" />
          </Section>
        ) : null}

        {data.chain.length ? (
          <Section id="pay-chain" title="Invoice, payment, credit note and refund">
            <ChainList steps={data.chain} current={data.id} label="Refund chain" />
          </Section>
        ) : null}

        {data.importChain ? (
          <Section id="pay-import" title={data.importChain.name || "Import"}>
            <ImportChainView chain={data.importChain} />
          </Section>
        ) : null}

        {data.history.length ? (
          <Section id="pay-history" title="What happened">
            <div className="card">
              <Timeline steps={data.history} />
            </div>
          </Section>
        ) : null}
      </div>
    </div>
  );
}
