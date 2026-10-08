"use client";

import { useId, useRef, useState } from "react";
import detail from "@/components/detail/detail.module.css";
import { Facts, money, OriginalIds, Pill, Result, Section } from "@/components/detail/parts";
import { send, useApi } from "@/components/detail/useApi";
import { Icon } from "@/components/Icon";
import { Loading } from "@/components/live/Loading";
import { Bullets, Disclosure } from "@/components/ui";
import { filePayload } from "@/lib/api";
import { formatDay } from "@/lib/format";
import type { Obligation, ObligationsData } from "@/lib/types";

function dueWords(o: Obligation, today: string): string {
  const when = formatDay(o.due);
  if (o.status === "done") return `Was due ${when}`;
  if (o.due < today) return `Was due ${when}`;
  if (o.due === today) return "Due today";
  return `Due ${when}`;
}

/**
 * Every deadline from a letter or a message (§24): who does it, what proves it done, how it stands, and,
 * when only the owner can know, "It is done" with the confirmations the engine offers.
 */
export function DeadlinesView() {
  const { data, error, loading, reload } = useApi<ObligationsData>("/api/obligations");
  const [notice, setNotice] = useState<string | null>(null);
  const onDone = (text: string) => {
    setNotice(text);
    reload();
  };
  if (loading) return <Loading />;
  if (!data) {
    return (
      <div className="container-narrow page">
        <p className="card card-pad muted" role="status">
          {error ?? "This page didn’t load."}
        </p>
      </div>
    );
  }
  const open = data.items.filter((o) => o.status === "open");
  const info = data.items.filter((o) => o.status === "information");
  const done = data.items.filter((o) => o.status === "done");
  const late = open.filter((o) => o.due < data.today).length;

  return (
    <div className="container-narrow page">
      <header className="page-head">
        <h1 className="h1">Deadlines</h1>
        <p className="lead">
          {open.length === 0
            ? "Nothing is due. I add every deadline I find in your letters and messages."
            : `${open.length} open${late ? `, ${late} late` : ""}. I close each one only when I have the proof.`}
        </p>
      </header>

      <div className={detail.stack}>
        <p className={notice ? "notice" : detail.resultEmpty} role="status" aria-live="polite">
          {notice ? (
            <>
              <Icon name="check" size={18} />
              <span>{notice}</span>
            </>
          ) : null}
        </p>
        {open.length ? (
          <Section id="due-open" title="Coming up">
            <ul className="stack-2">
              {open.map((o) => (
                <DeadlineCard key={o.id} o={o} today={data.today} onDone={onDone} />
              ))}
            </ul>
          </Section>
        ) : null}

        {info.length ? (
          <Section id="due-info" title="For your information">
            <ul className="stack-2">
              {info.map((o) => (
                <DeadlineCard key={o.id} o={o} today={data.today} onDone={onDone} />
              ))}
            </ul>
          </Section>
        ) : null}

        {done.length ? (
          <Section id="due-done" title="Done" aside={`${done.length}`}>
            <Disclosure summary={done.length === 1 ? "Show the one that is done" : `Show the ${done.length} that are done`}>
              <ul className="stack-2">
                {done.map((o) => (
                  <DeadlineCard key={o.id} o={o} today={data.today} onDone={onDone} />
                ))}
              </ul>
            </Disclosure>
          </Section>
        ) : null}

        {data.items.length === 0 ? <p className="card card-pad muted">No deadlines yet.</p> : null}
      </div>
    </div>
  );
}

function DeadlineCard({ o, today, onDone }: { o: Obligation; today: string; onDone: (text: string) => void }) {
  const late = o.status === "open" && o.due < today;
  const tone = o.status === "done" ? "good" : late ? "risk" : o.status === "information" ? "neutral" : "attention";
  return (
    <li className="card card-pad stack-2" aria-labelledby={`${o.id}-t`}>
      <div className={detail.rowLine}>
        <div className={detail.rowMain}>
          <h3 id={`${o.id}-t`} className="h3">
            {o.title}
          </h3>
          <span className="meta">{[o.companyName, o.reference ? `reference ${o.reference}` : null].filter(Boolean).join(" · ")}</span>
        </div>
        {o.amount !== null ? <span className={`num ${detail.rowAmount}`}>{money(o.amount, o.currency)}</span> : null}
      </div>
      <div className={detail.pills}>
        <Pill tone={tone}>
          {o.status === "done" ? "Done" : late ? "Late" : o.status === "information" ? "For information" : dueWords(o, today)}
        </Pill>
        {o.status !== "open" || late ? <span className="meta">{dueWords(o, today)}</span> : null}
      </div>
      <Facts
        items={[
          { label: "Who does it", value: o.responsible },
          { label: "What proves it done", value: o.condition || o.requiredProof },
          { label: "If it is missed", value: o.status === "open" ? o.consequence : "" },
        ]}
      />
      {o.nextStep ? (
        <p className={detail.nextStep}>
          <Icon name="clock" size={16} />
          <span>{o.nextStep}</span>
        </p>
      ) : null}
      {o.status === "open" && o.confirmOptions.length ? <ItIsDone o={o} onDone={onDone} /> : null}
      <Disclosure summary="Why?">
        <div className="stack-2">
          <Bullets items={o.why} />
          <OriginalIds ids={o.evidenceIds} label={(i) => (i === 0 ? "The letter" : "The proof")} />
        </div>
      </Disclosure>
    </li>
  );
}

/** The owner says it is done, with one of the confirmations the engine offers (and the proof, if they have it). */
function ItIsDone({ o, onDone }: { o: Obligation; onDone: (text: string) => void }) {
  const ids = useId();
  const fileRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [outcome, setOutcome] = useState(o.confirmOptions.length === 1 ? (o.confirmOptions[0]?.id ?? "") : "");
  const [until, setUntil] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const chosen = o.confirmOptions.find((c) => c.id === outcome);

  const submit = async (ev: React.FormEvent) => {
    ev.preventDefault();
    if (!chosen) return;
    setBusy(true);
    setResult(null);
    const file = fileRef.current?.files?.[0];
    const body: Record<string, unknown> = { outcome: chosen.id };
    if (chosen.needsDate && until) body.validUntil = until;
    if (file) Object.assign(body, await filePayload(file));
    const r = await send(`/api/obligations/${encodeURIComponent(o.id)}/done`, body);
    setBusy(false);
    if (r.ok) onDone(r.message);
    else setResult({ ok: false, text: r.message });
  };

  if (!open) {
    return (
      <div className={detail.actions}>
        <button type="button" className="btn btn-primary" onClick={() => setOpen(true)} aria-expanded={false} aria-controls={`${ids}-form`}>
          It is done
        </button>
      </div>
    );
  }
  return (
    <form id={`${ids}-form`} className={detail.form} onSubmit={submit} aria-label={`${o.title}: it is done`}>
      <fieldset className={detail.form} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
        <legend className="h3" style={{ padding: 0, marginBottom: 8 }}>
          What happened?
        </legend>
        {o.confirmOptions.map((c) => (
          <label key={c.id} className="choice">
            <input type="radio" name={`${ids}-outcome`} value={c.id} checked={outcome === c.id} onChange={() => setOutcome(c.id)} />
            <span className="radio-mark" aria-hidden="true" />
            {c.label}
          </label>
        ))}
      </fieldset>
      <div className={detail.fields}>
        {chosen?.needsDate ? (
          <div className={detail.field}>
            <label className="label" htmlFor={`${ids}-until`}>
              Valid until
            </label>
            <input id={`${ids}-until`} className="input" type="date" value={until} required onChange={(e) => setUntil(e.target.value)} />
          </div>
        ) : null}
        <div className={detail.field}>
          <label className="label" htmlFor={`${ids}-file`}>
            The proof <span className="meta">(optional)</span>
          </label>
          <input id={`${ids}-file`} ref={fileRef} className="input" type="file" accept="image/*,application/pdf,text/plain,message/rfc822" />
        </div>
      </div>
      <p className={detail.hint}>Your confirmation is kept as the proof, with the file if you add one.</p>
      <div className={detail.actions}>
        <button type="submit" className="btn btn-primary" disabled={busy || !chosen}>
          {busy ? "One moment…" : "Confirm"}
        </button>
        <button type="button" className="btn btn-secondary" onClick={() => setOpen(false)}>
          Not yet
        </button>
      </div>
      <Result result={result} />
    </form>
  );
}
