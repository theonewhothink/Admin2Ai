"use client";

import { useState } from "react";
import detail from "@/components/detail/detail.module.css";
import { BackLink, day, Pill, Result, Section } from "@/components/detail/parts";
import { send, useApi } from "@/components/detail/useApi";
import { Icon } from "@/components/Icon";
import { Loading } from "@/components/live/Loading";
import { Dot, Progress } from "@/components/ui";
import { formatMonth, formatNumber } from "@/lib/format";
import { browserEngine } from "@/lib/mode";
import type { BillingData, BillingPlan } from "@/lib/types";

const PAID = new Set(["solo", "business", "multi", "accountant"]);
const DEMO_NOTE = "This is the demo, so nothing is billed and no payment page opens. In your own account this opens a secure payment page.";

/** The payment provider's page: an absolute https address (http only for a server on this computer). */
function safeUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    const local = url.hostname === "localhost" || url.hostname === "127.0.0.1";
    return url.protocol === "https:" || (url.protocol === "http:" && local) ? url.href : null;
  } catch {
    return null;
  }
}

function leave(url: string) {
  // The payment provider's own page (card details never pass through the back office).
  window.location.assign(url);
}

function UsageRow({ label, used, limit }: { label: string; used: number | null; limit: number | null }) {
  if (used === null) return null;
  const over = limit !== null && used > limit;
  return (
    <div className={detail.usageRow}>
      <div className={detail.usageLine}>
        <span>{label}</span>
        <span className={`num ${over ? "attention-text" : ""}`}>
          {formatNumber(used)}
          {limit !== null ? ` of ${formatNumber(limit)}` : " · no limit"}
        </span>
      </div>
      {limit !== null ? (
        <Progress value={limit ? (used / limit) * 100 : 100} tone={over ? "attention" : undefined} label={`${label}: ${used} of ${limit}`} />
      ) : null}
    </div>
  );
}

function limitsLine(p: BillingPlan): string {
  const l = p.limits;
  const parts = [
    l.companies === null ? "Any number of companies" : `${l.companies} ${l.companies === 1 ? "company" : "companies"}`,
    l.documentsPerMonth === null ? "documents without limit" : `${formatNumber(l.documentsPerMonth)} documents a month`,
    l.users === null ? "" : `${l.users} ${l.users === 1 ? "person" : "people"}`,
  ];
  return parts.filter(Boolean).join(" · ");
}

/** The plan, what the business uses this month, and changing plan or payment details (Stripe's own pages). */
export function PlanView() {
  const { data, error, loading } = useApi<BillingData>("/api/billing");
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  if (loading) return <Loading />;
  if (!data) {
    return (
      <div className="container-narrow page">
        <BackLink href="/settings" label="Settings" />
        <p className="card card-pad muted" role="status">
          {error ?? "This page didn’t load."}
        </p>
      </div>
    );
  }

  // The static demo never calls the payment provider, whatever the engine says.
  const demo = data.demo || browserEngine;
  const current = data.plan.id;

  const choose = async (plan: BillingPlan) => {
    setResult(null);
    if (demo) {
      setResult({ ok: true, text: `${plan.name}: ${DEMO_NOTE}` });
      return;
    }
    if (!data.canUpgrade) {
      setResult({ ok: false, text: "Payments are not set up here yet. Nothing was charged." });
      return;
    }
    setBusy(plan.id);
    const r = await send<{ url?: string }>("/api/billing/checkout", { plan: plan.id });
    const url = r.ok ? safeUrl(r.body.url) : null;
    if (url) {
      setResult({ ok: true, text: r.body.message ?? "Opening the payment page…" });
      leave(url);
      return;
    }
    setBusy(null);
    setResult({ ok: false, text: r.ok ? "The payment page did not open. Please try again in a few minutes." : r.message });
  };

  const manage = async () => {
    setResult(null);
    if (demo) {
      setResult({ ok: true, text: DEMO_NOTE });
      return;
    }
    setBusy("portal");
    const r = await send<{ url?: string }>("/api/billing/portal", {});
    const url = r.ok ? safeUrl(r.body.url) : null;
    if (url) {
      leave(url);
      return;
    }
    setBusy(null);
    setResult({ ok: false, text: r.ok ? "The billing page did not open. Please try again in a few minutes." : r.message });
  };

  return (
    <div className="container-narrow page">
      <BackLink href="/settings" label="Settings" />
      <header className="page-head">
        <h1 className="h1">Your plan</h1>
        <p className="lead">{data.plan.price === data.plan.name ? data.plan.name : `${data.plan.name} · ${data.plan.price}`}</p>
      </header>

      <div className={detail.stack}>
        <div className="card card-pad stack-2">
          <div className="row-between" style={{ flexWrap: "wrap" }}>
            <p className="h3">{data.plan.name}</p>
            {demo ? <Pill tone="neutral">Demo</Pill> : data.plan.status === "past_due" ? <Pill tone="attention">Payment failed</Pill> : null}
          </div>
          <p className="muted">{demo ? (data.message ?? "This is the demo business, so nothing here is billed.") : data.plan.summary}</p>
          {data.plan.renewsOn && !demo ? <p className="meta">Renews on {day(data.plan.renewsOn)}.</p> : null}
        </div>

        {data.notice ? (
          <div className="notice notice-attention">
            <Dot tone="attention" />
            <p>{data.notice}</p>
          </div>
        ) : null}
        {data.prompt ? (
          <div className="notice notice-attention">
            <Dot tone="attention" />
            <p>{data.prompt}</p>
          </div>
        ) : null}
        {data.held && data.waiting ? (
          <div className="notice notice-attention">
            <Dot tone="attention" />
            <p>
              {data.waiting === 1 ? "One new document waits" : `${formatNumber(data.waiting)} new documents wait`}, kept safely,
              until you change plan or the month turns. Nothing is lost.
            </p>
          </div>
        ) : null}

        <Section id="usage" title={`Used in ${formatMonth(data.usage.month)}`}>
          <div className={`card card-pad ${detail.usage}`}>
            <UsageRow label="Companies" used={data.usage.companies} limit={data.limits.companies} />
            <UsageRow label="Documents this month" used={data.usage.documents} limit={data.limits.documentsPerMonth} />
            <UsageRow label="People with access" used={data.usage.users} limit={data.limits.users} />
            {data.usage.clients ? <UsageRow label="Active clients" used={data.usage.clients} limit={null} /> : null}
          </div>
        </Section>

        <Section id="plans" title="Plans">
          <ul className={detail.plans}>
            {data.plans.map((p) => {
              const mine = p.id === current;
              return (
                <li key={p.id} className={`card ${detail.plan}`} data-current={mine}>
                  <span className={detail.planName}>{p.name}</span>
                  {p.price !== p.name ? <span className={`num ${detail.planPrice}`}>{p.price}</span> : null}
                  <span className="meta">{p.summary}</span>
                  <span className="meta">{limitsLine(p)}</span>
                  <span className={detail.planAction}>
                    {mine ? (
                      <Pill tone="neutral">Your plan</Pill>
                    ) : PAID.has(p.id) ? (
                      <button
                        type="button"
                        className="btn btn-secondary"
                        disabled={busy !== null}
                        onClick={() => void choose(p)}
                        aria-label={`Choose ${p.name}`}
                      >
                        {busy === p.id ? "Opening…" : `Choose ${p.name}`}
                      </button>
                    ) : null}
                  </span>
                </li>
              );
            })}
          </ul>
          <Result result={result} />
        </Section>

        <Section id="payment" title="Payment details">
          <div className={`card card-pad ${detail.form}`}>
            <p className="muted">
              Your card, invoices and cancelling are on Stripe’s secure page. The back office never sees your card number.
            </p>
            <div className={detail.actions}>
              <button type="button" className="btn btn-secondary" disabled={busy !== null || (!demo && !data.canManage)} onClick={() => void manage()}>
                <Icon name="lock" size={18} />
                {busy === "portal" ? "Opening…" : "Manage payment details"}
              </button>
            </div>
            {!demo && !data.canManage ? <p className="meta">Available once you are on a paid plan.</p> : null}
          </div>
        </Section>
      </div>
    </div>
  );
}
