"use client";

/**
 * Onboarding in production (spec §4): the account already exists (sign-up made
 * it, with the first company), so the steps are Company, Email, Bank,
 * Accountant, Start, each wired to the real API:
 *
 *   Company     POST /api/onboarding/company {name, taxId}      (more companies)
 *   Email       POST /api/settings/reading {history}           (how far back I read: 90 days or 12 months)
 *               GET  /api/oauth/start?provider=google|microsoft (full-page consent)
 *   Bank        POST /api/connections/bank/start {institutionId} → redirectUrl
 *   Accountant  POST /api/onboarding/accountant {email, name?}
 *
 * The step is kept in sessionStorage, so the flow resumes after the round
 * trip to the provider or the bank. Connections are read from GET /api/home.
 * Nothing is pretended: "Connected" is shown only when the API lists the
 * connection. The demo keeps its own timer-based flow (OnboardingFlow).
 */
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Field, FormAlert } from "@/components/auth/Field";
import { Icon } from "@/components/Icon";
import { useApi } from "@/components/detail/useApi";
import { useData } from "@/components/live/useData";
import { HistoryChoice } from "@/components/settings/HistoryChoice";
import { addCompany, chooseHistory, oauthStartUrl, requireSession, setAccountant, startBankConnection } from "@/lib/account";
import { getCompanies, getHome } from "@/lib/api";
import { banks } from "@/lib/banks";
import { checkNif } from "@/lib/nif";
import { clearProgress, saveProgress, savedStep, type Leg } from "@/lib/onboarding-progress";
import type { CompanySummary, Connection, HistoryChoice as Choice, ReadingData } from "@/lib/types";
import styles from "./flow.module.css";
import { ProviderButton, StepHead } from "./OnboardingFlow";
import { OnboardingReturn } from "./OnboardingReturn";

const STEPS = ["Company", "Email", "Bank", "Accountant", "Start"] as const;

const load = async () => {
  const session = await requireSession();
  const [home, companies] = await Promise.all([getHome(), getCompanies()]);
  return { session, connections: home.connections, companies };
};

const noSubscribe = () => () => undefined;
const onServer = () => -1;

type Returned = { leg: Leg; result: "done" | "failed" | "back" } | null;

function useReturned(): Returned {
  const params = useSearchParams();
  const leg = params.get("returned");
  const result = params.get("result");
  if ((leg === "email" || leg === "bank") && (result === "done" || result === "failed" || result === "back")) return { leg, result };
  return null;
}

export function OnboardingSetup() {
  const router = useRouter();
  const data = useData(load);
  const saved = useSyncExternalStore(noSubscribe, savedStep, onServer);
  const [chosen, setChosen] = useState<number | null>(null);
  const returned = useReturned();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const step = chosen ?? Math.max(saved, 0);

  // Move focus to the new step's heading so keyboard and screen-reader users follow along.
  useEffect(() => {
    if (chosen !== null) headingRef.current?.focus();
  }, [chosen]);

  const go = (next: number) => {
    const clamped = Math.min(Math.max(next, 0), STEPS.length - 1);
    saveProgress({ step: clamped });
    setChosen(clamped);
  };

  /** Leave for a consent page; come back to this step. */
  const leave = (leg: Leg, url: string) => {
    saveProgress({ step, left: leg, leftAt: Date.now() });
    window.location.assign(url);
  };

  if (!data) {
    return (
      <div className={styles.flow}>
        {/* The provider may send the owner straight back here (…/onboarding?ref=…). */}
        <OnboardingReturn />
        <p className="muted" role="status">
          One moment…
        </p>
      </div>
    );
  }

  const email = data.connections.find((c) => c.kind === "email");
  const bank = data.connections.find((c) => c.kind === "bank");

  return (
    <div className={styles.flow}>
      <div className={styles.progressRow}>
        {step > 0 ? (
          <button type="button" className="btn btn-quiet" onClick={() => go(step - 1)}>
            <Icon name="chevronLeft" size={16} />
            Back
          </button>
        ) : (
          <span />
        )}
        <span className="meta num">
          Step {step + 1} of {STEPS.length}
        </span>
      </div>
      <ol className={styles.steps} aria-label="Progress" style={{ gridTemplateColumns: `repeat(${STEPS.length}, 1fr)` }}>
        {STEPS.map((s, i) => (
          <li key={s} data-state={i < step ? "done" : i === step ? "current" : "todo"}>
            <span className="visually-hidden">
              {s}
              {i < step ? ", done" : i === step ? ", current step" : ""}
            </span>
          </li>
        ))}
      </ol>

      <div key={step} className={styles.stepBody}>
        {step === 0 && (
          <CompanyStep headingRef={headingRef} companies={data.companies} tenantName={data.session.tenant.name} onNext={() => go(1)} />
        )}
        {step === 1 && (
          <EmailStep
            headingRef={headingRef}
            connection={email}
            failed={returned?.leg === "email" && returned.result === "failed"}
            onConnect={(provider) => leave("email", oauthStartUrl(provider))}
            onNext={() => go(2)}
          />
        )}
        {step === 2 && (
          <BankStep
            headingRef={headingRef}
            connection={bank}
            failed={returned?.leg === "bank" && returned.result === "failed"}
            onLeave={(url) => leave("bank", url)}
            onNext={() => go(3)}
          />
        )}
        {step === 3 && <AccountantStep headingRef={headingRef} onNext={() => go(4)} />}
        {step === 4 && (
          <StartStep
            headingRef={headingRef}
            onStart={() => {
              clearProgress();
              router.push("/");
            }}
          />
        )}
      </div>
    </div>
  );
}

interface StepProps {
  headingRef: React.RefObject<HTMLHeadingElement | null>;
  onNext: () => void;
}

function Later({ onClick }: { onClick: () => void }) {
  return (
    <button type="button" className="btn btn-quiet" onClick={onClick} style={{ justifySelf: "center" }}>
      I’ll do this later
    </button>
  );
}

function ConnectedLine({ connection }: { connection: Connection }) {
  const healthy = connection.status === "healthy";
  return (
    <p className={healthy ? styles.connectedLine : "attention-text"} role="status">
      <Icon name={healthy ? "check" : "refresh"} size={18} strokeWidth={2.2} />
      {healthy
        ? `${connection.name} connected${connection.account ? `: ${connection.account}` : ""}.`
        : `${connection.name} needs reconnecting.`}
    </p>
  );
}

/* ---------- 1. Company ---------- */

function CompanyStep({
  headingRef,
  companies,
  tenantName,
  onNext,
}: StepProps & { companies: CompanySummary[]; tenantName: string }) {
  const [added, setAdded] = useState<{ name: string; taxId: string }[]>([]);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [taxId, setTaxId] = useState("");
  const [errors, setErrors] = useState<{ name?: string | null; taxId?: string | null }>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);
  const taxRef = useRef<HTMLInputElement>(null);
  const known = companies.length > 0 ? companies.map((c) => ({ key: c.id, name: c.name })) : tenantName ? [{ key: "tenant", name: tenantName }] : [];

  useEffect(() => {
    if (adding) nameRef.current?.focus();
  }, [adding]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const tax = checkNif(taxId);
    const found = { name: name.trim() ? null : "Enter the company’s name.", taxId: tax.valid ? null : tax.message };
    setErrors(found);
    setFormError(null);
    if (found.name) return nameRef.current?.focus();
    if (found.taxId || !tax.valid) return taxRef.current?.focus();
    setBusy(true);
    const r = await addCompany({ name, taxId: tax.normalized });
    setBusy(false);
    if (!r.ok) {
      if (r.field === "taxId") {
        setErrors({ taxId: r.message });
        taxRef.current?.focus();
      } else setFormError(r.message);
      return;
    }
    setAdded((a) => [...a, { name: name.trim(), taxId: tax.normalized }]);
    setName("");
    setTaxId("");
    setAdding(false);
  };

  return (
    <>
      <StepHead headingRef={headingRef} title="Your companies" text="These are the businesses I look after. More than one? Add the others now." />
      <ul className="card list" aria-label="Your companies">
        {[...known, ...added.map((a) => ({ key: `added-${a.taxId}`, name: a.name }))].map((c) => (
          <li key={c.key} className="list-row">
            <Icon name="building" size={20} style={{ color: "var(--text-2)" }} />
            <span style={{ flex: 1, fontWeight: 600 }}>{c.name}</span>
            <span className={styles.connected}>
              <Icon name="check" size={16} strokeWidth={2.2} />
              Added
            </span>
          </li>
        ))}
      </ul>

      {adding ? (
        <form className={`card card-pad stack-3 ${styles.reveal}`} onSubmit={submit} noValidate aria-label="Add a company">
          <FormAlert id="company-error" message={formError} />
          <Field
            id="company-name"
            label="Company name"
            autoComplete="organization"
            value={name}
            inputRef={nameRef}
            error={errors.name}
            onChange={(e) => setName(e.target.value)}
          />
          <Field
            id="company-nif"
            label="Tax number (NIF)"
            inputMode="numeric"
            autoComplete="off"
            spellCheck={false}
            placeholder="123 456 789"
            hint="9 digits."
            value={taxId}
            inputRef={taxRef}
            error={errors.taxId}
            onChange={(e) => {
              setTaxId(e.target.value);
              if (errors.taxId) setErrors((x) => ({ ...x, taxId: null }));
            }}
            onBlur={() => {
              if (taxId.trim()) {
                const tax = checkNif(taxId);
                setErrors((x) => ({ ...x, taxId: tax.valid ? null : tax.message }));
              }
            }}
          />
          <div className="row">
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy ? "Adding…" : "Add company"}
            </button>
            <button type="button" className="btn btn-quiet" onClick={() => setAdding(false)} disabled={busy}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <button type="button" className="choice" onClick={() => setAdding(true)}>
          <Icon name="building" size={18} style={{ color: "var(--text-2)" }} />
          Add another company
        </button>
      )}

      <button type="button" className="btn btn-primary btn-lg btn-block" onClick={onNext}>
        Continue
      </button>
    </>
  );
}

/* ---------- 2. Email ---------- */

function EmailStep({
  headingRef,
  connection,
  failed,
  onConnect,
  onNext,
}: StepProps & { connection?: Connection; failed: boolean; onConnect: (provider: "google" | "microsoft") => void }) {
  const [leaving, setLeaving] = useState<string | null>(null);
  // How far back the first read of the email and the bank goes (spec §6): saved before connecting.
  const reading = useApi<ReadingData>("/api/settings/reading");
  const [history, setHistory] = useState<Choice | null>(null);
  const [historyBusy, setHistoryBusy] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const chosen = history ?? reading.data?.history ?? "90d";
  const choose = async (choice: Choice) => {
    const before = chosen;
    setHistory(choice);
    setHistoryError(null);
    setHistoryBusy(true);
    const r = await chooseHistory(choice);
    setHistoryBusy(false);
    if (!r.ok) {
      setHistory(before);
      setHistoryError(r.message);
    }
  };
  const connect = (provider: "google" | "microsoft") => {
    setLeaving(provider);
    onConnect(provider);
  };
  return (
    <>
      <StepHead
        headingRef={headingRef}
        title="Connect your email"
        text="I look for invoices, receipts and supplier messages. I never send anything without asking you first."
      />
      {failed && !connection ? (
        <p className="notice notice-attention" role="alert">
          I couldn’t connect your email. Try again, or choose the other provider.
        </p>
      ) : null}
      {connection ? <ConnectedLine connection={connection} /> : null}
      <HistoryChoice value={chosen} disabled={historyBusy || leaving !== null} onChange={(c) => void choose(c)} />
      {historyError ? (
        <p className="attention-text" role="alert">
          {historyError}
        </p>
      ) : null}
      <div className="stack-1">
        <ProviderButton
          icon="google"
          label={leaving === "google" ? "Opening Google…" : "Google"}
          sub="Gmail or Google Workspace"
          onClick={() => connect("google")}
          state={leaving ? "working" : "idle"}
        />
        <ProviderButton
          icon="microsoft"
          label={leaving === "microsoft" ? "Opening Microsoft…" : "Microsoft"}
          sub="Outlook or Microsoft 365"
          onClick={() => connect("microsoft")}
          state={leaving ? "working" : "idle"}
        />
      </div>
      {connection ? (
        <button type="button" className="btn btn-primary btn-lg btn-block" onClick={onNext}>
          Continue
        </button>
      ) : (
        <Later onClick={onNext} />
      )}
    </>
  );
}

/* ---------- 3. Bank ---------- */

function BankStep({
  headingRef,
  connection,
  failed,
  onLeave,
  onNext,
}: StepProps & { connection?: Connection; failed: boolean; onLeave: (url: string) => void }) {
  const [picked, setPicked] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(failed ? "Your bank didn’t confirm the connection. Try again." : null);

  const open = async (institutionId: string, name: string) => {
    setPicked(name);
    setMessage(null);
    const r = await startBankConnection(institutionId);
    if (r.ok) {
      onLeave(r.value);
      return;
    }
    setPicked(null);
    setMessage(r.message);
  };

  return (
    <>
      <StepHead
        headingRef={headingRef}
        title="Connect your bank"
        text="Read-only, through Open Banking. I can see transactions. I can never move money."
      />
      {connection ? <ConnectedLine connection={connection} /> : null}
      <div className={styles.bankGrid}>
        {banks.map((b) => (
          <button
            key={b.institutionId}
            type="button"
            className="choice"
            data-selected={picked === b.name}
            disabled={picked !== null}
            onClick={() => void open(b.institutionId, b.name)}
          >
            <Icon name="bank" size={18} style={{ color: "var(--text-2)" }} />
            {b.name}
          </button>
        ))}
      </div>
      <div aria-live="polite" className={styles.bankStatus}>
        {picked ? <p className="muted">Opening {picked}…</p> : null}
        {message ? <p className="attention-text">{message}</p> : null}
      </div>
      {connection ? (
        <button type="button" className="btn btn-primary btn-lg btn-block" onClick={onNext}>
          Continue
        </button>
      ) : (
        <Later onClick={onNext} />
      )}
    </>
  );
}

/* ---------- 4. Accountant ---------- */

function AccountantStep({ headingRef, onNext }: StepProps) {
  const [mode, setMode] = useState<"invite" | "none" | null>(null);
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [invited, setInvited] = useState<string | null>(null);
  const emailRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (mode === "invite") emailRef.current?.focus();
  }, [mode]);

  const invite = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const valid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
    setError(valid ? null : "Enter your accountant’s email address.");
    setFormError(null);
    if (!valid) return emailRef.current?.focus();
    setBusy(true);
    const r = await setAccountant({ email, name });
    setBusy(false);
    if (r.ok) setInvited(email.trim());
    else if (r.field === "email") setError(r.message);
    else setFormError(r.message);
  };

  const ready = mode === "none" || invited !== null;
  return (
    <>
      <StepHead
        headingRef={headingRef}
        title="Your accountant"
        text="I prepare everything they need and answer their questions, so you don’t have to."
      />
      <div className="stack-1">
        <button type="button" className="choice" data-selected={mode === "invite"} aria-pressed={mode === "invite"} onClick={() => setMode("invite")}>
          <span className="radio-mark" aria-hidden="true" />
          Invite them by email
        </button>
        {mode === "invite" ? (
          invited ? (
            <p className={`${styles.connectedLine} ${styles.nested}`} role="status">
              <Icon name="check" size={18} strokeWidth={2.2} />
              {invited} is your accountant.
            </p>
          ) : (
            <form className={`card card-pad stack-2 ${styles.reveal} ${styles.nested}`} onSubmit={invite} noValidate aria-label="Your accountant">
              <FormAlert id="accountant-error" message={formError} />
              <Field
                id="acc-email"
                label="Their email"
                type="email"
                autoComplete="off"
                inputMode="email"
                autoCapitalize="none"
                spellCheck={false}
                value={email}
                inputRef={emailRef}
                error={error}
                onChange={(e) => setEmail(e.target.value)}
              />
              <Field id="acc-name" label="Their name" optional autoComplete="off" value={name} onChange={(e) => setName(e.target.value)} />
              <div>
                <button type="submit" className="btn btn-secondary" disabled={busy}>
                  {busy ? "Saving…" : "Save accountant"}
                </button>
              </div>
            </form>
          )
        ) : null}
        <button type="button" className="choice" data-selected={mode === "none"} aria-pressed={mode === "none"} onClick={() => setMode("none")}>
          <span className="radio-mark" aria-hidden="true" />
          I don’t have an accountant
        </button>
      </div>
      <button type="button" className="btn btn-primary btn-lg btn-block" disabled={!ready} onClick={onNext}>
        Continue
      </button>
    </>
  );
}

/* ---------- 5. Start ---------- */

function StartStep({ headingRef, onStart }: { headingRef: StepProps["headingRef"]; onStart: () => void }) {
  return (
    <div className={styles.start}>
      <StepHead
        headingRef={headingRef}
        title="That’s everything."
        text="I will read your email and bank history and learn how your business works. It takes a few minutes."
      />
      <button type="button" className={styles.startButton} onClick={onStart}>
        Start
      </button>
      <p className="meta">You can close this page. I will keep going.</p>
    </div>
  );
}
