"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Icon, type IconName } from "@/components/Icon";
import { companyLookup, owner } from "@/lib/data";
import styles from "./flow.module.css";

const STEPS = ["Account", "Company", "Email", "Bank", "Accountant", "Start"] as const;

type Pending = "idle" | "working" | "done";

/** Small helper: flips idle → working → done after a pause. */
function useFakeTask(ms: number) {
  const [state, setState] = useState<Pending>("idle");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (timer.current) clearTimeout(timer.current);
  }, []);
  const start = () => {
    if (timer.current) clearTimeout(timer.current);
    setState("working");
    timer.current = setTimeout(() => setState("done"), ms);
  };
  const reset = () => {
    if (timer.current) clearTimeout(timer.current);
    setState("idle");
  };
  return { state, start, reset };
}

export function OnboardingFlow() {
  const router = useRouter();
  const [step, setStep] = useState(0);
  const headingRef = useRef<HTMLHeadingElement>(null);

  // Move focus to the new step's heading so keyboard and screen-reader users follow along.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    headingRef.current?.focus();
  }, [step]);

  const next = () => setStep((s) => Math.min(s + 1, STEPS.length - 1));
  const back = () => setStep((s) => Math.max(s - 1, 0));

  return (
    <div className={styles.flow}>
      <div className={styles.progressRow}>
        {step > 0 ? (
          <button type="button" className="btn btn-quiet" onClick={back}>
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
      <ol className={styles.steps} aria-label="Progress">
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
        {step === 0 && <AccountStep headingRef={headingRef} onNext={next} />}
        {step === 1 && <CompanyStep headingRef={headingRef} onNext={next} />}
        {step === 2 && <EmailStep headingRef={headingRef} onNext={next} />}
        {step === 3 && <BankStep headingRef={headingRef} onNext={next} />}
        {step === 4 && <AccountantStep headingRef={headingRef} onNext={next} />}
        {step === 5 && <StartStep headingRef={headingRef} onStart={() => router.push("/onboarding/learning")} />}
      </div>
    </div>
  );
}

interface StepProps {
  headingRef: React.RefObject<HTMLHeadingElement | null>;
  onNext: () => void;
}

function StepHead({ headingRef, title, text }: { headingRef: StepProps["headingRef"]; title: string; text: string }) {
  return (
    <div className={styles.stepHead}>
      <h1 ref={headingRef} tabIndex={-1} className="h1">
        {title}
      </h1>
      <p className="lead">{text}</p>
    </div>
  );
}

function ProviderButton({
  icon,
  label,
  sub,
  onClick,
  state,
  selected,
}: {
  icon: IconName | "google" | "microsoft";
  label: string;
  sub?: string;
  onClick: () => void;
  state?: Pending;
  selected?: boolean;
}) {
  return (
    <button
      type="button"
      className={`choice ${styles.provider}`}
      data-selected={selected}
      onClick={onClick}
      disabled={state === "working"}
    >
      <span className={styles.providerIcon}>
        {icon === "google" ? (
          <Monogram letter="G" />
        ) : icon === "microsoft" ? (
          <Monogram letter="M" />
        ) : (
          <Icon name={icon} size={20} />
        )}
      </span>
      <span className={styles.providerText}>
        <span>{label}</span>
        {sub ? <span className="meta">{sub}</span> : null}
      </span>
      {selected && state === "working" ? <span className="meta">Connecting…</span> : null}
      {selected && state === "done" ? (
        <span className={styles.connected}>
          <Icon name="check" size={16} strokeWidth={2.2} />
          Connected
        </span>
      ) : null}
    </button>
  );
}

/* Neutral monograms: recognisable without importing brand colour into the page. */
function Monogram({ letter }: { letter: string }) {
  return (
    <span className={styles.monogram} aria-hidden="true">
      {letter}
    </span>
  );
}

/* ---------- 1. Account ---------- */

function AccountStep({ headingRef, onNext }: StepProps) {
  const [email, setEmail] = useState("");
  const valid = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
  return (
    <>
      <StepHead headingRef={headingRef} title="Create your account" text="It takes about three minutes. After that, I do the work." />
      <div className="stack-1">
        <ProviderButton icon="google" label="Continue with Google" onClick={onNext} />
        <ProviderButton icon="microsoft" label="Continue with Microsoft" onClick={onNext} />
      </div>
      <div className={styles.or}>
        <span>or</span>
      </div>
      <form
        className="stack-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) onNext();
        }}
      >
        <div>
          <label htmlFor="ob-email" className="label">
            Work email
          </label>
          <input
            id="ob-email"
            className="input"
            type="email"
            autoComplete="email"
            placeholder={owner.email}
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={!valid}>
          Continue
        </button>
      </form>
    </>
  );
}

/* ---------- 2. Company ---------- */

function CompanyStep({ headingRef, onNext }: StepProps) {
  const [value, setValue] = useState("");
  const lookup = useFakeTask(900);
  const clean = value.replace(/[\s.-]/g, "").toUpperCase();
  const valid = /^(ES)?[A-Z0-9]{8,10}$/.test(clean);
  return (
    <>
      <StepHead
        headingRef={headingRef}
        title="Your company number"
        text="Your VAT number or NIF. I will fill in the rest from public records."
      />
      <form
        className={styles.inline}
        onSubmit={(e) => {
          e.preventDefault();
          if (valid) lookup.start();
        }}
      >
        <label htmlFor="ob-nif" className="visually-hidden">
          VAT number or NIF
        </label>
        <input
          id="ob-nif"
          className="input mono"
          placeholder="B67284519"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            lookup.reset();
          }}
        />
        <button type="submit" className="btn btn-primary" disabled={!valid || lookup.state === "working"}>
          {lookup.state === "working" ? "Looking it up…" : "Find my company"}
        </button>
      </form>
      {lookup.state === "idle" && value === "" ? (
        <button type="button" className={`link-quiet ${styles.fill}`} onClick={() => setValue(companyLookup.taxId)}>
          Use the sample company
        </button>
      ) : null}

      {lookup.state === "done" ? (
        <div className={`card ${styles.found}`} aria-live="polite">
          <div className={styles.foundHead}>
            <span className={styles.foundIcon}>
              <Icon name="building" size={20} />
            </span>
            <div className="stack-1">
              <p className="h3">{companyLookup.legalName}</p>
              <p className="meta num">{clean}</p>
            </div>
          </div>
          <dl className={styles.foundFacts}>
            <div>
              <dt>Address</dt>
              <dd>{companyLookup.address}</dd>
            </div>
            <div>
              <dt>Activity</dt>
              <dd>{companyLookup.activity}</dd>
            </div>
            <div>
              <dt>Tax</dt>
              <dd>{companyLookup.registeredSince}</dd>
            </div>
          </dl>
          <div className={styles.foundActions}>
            <button type="button" className="btn btn-primary" onClick={onNext}>
              That’s us
            </button>
            <button
              type="button"
              className="btn btn-quiet"
              onClick={() => {
                setValue("");
                lookup.reset();
              }}
            >
              Not right
            </button>
          </div>
          <p className="meta">More than one company? You can add the others later.</p>
        </div>
      ) : null}
    </>
  );
}

/* ---------- 3. Email ---------- */

function EmailStep({ headingRef, onNext }: StepProps) {
  const [provider, setProvider] = useState<string | null>(null);
  const task = useFakeTask(1100);
  const [imapEmail, setImapEmail] = useState("");
  const [imapPass, setImapPass] = useState("");

  const choose = (id: string) => {
    setProvider(id);
    if (id !== "imap") task.start();
    else task.reset();
  };

  return (
    <>
      <StepHead
        headingRef={headingRef}
        title="Connect your email"
        text="I look for invoices, receipts and supplier messages. I never send anything without asking you first."
      />
      <div className="stack-1">
        <ProviderButton icon="google" label="Google" sub="Gmail or Google Workspace" onClick={() => choose("google")} state={task.state} selected={provider === "google"} />
        <ProviderButton icon="microsoft" label="Microsoft" sub="Outlook or Microsoft 365" onClick={() => choose("microsoft")} state={task.state} selected={provider === "microsoft"} />
        <ProviderButton icon="mail" label="Another provider" sub="Any inbox that supports IMAP" onClick={() => choose("imap")} state={task.state} selected={provider === "imap"} />
      </div>

      {provider === "imap" && task.state !== "done" ? (
        <form
          className={`card card-pad stack-2 ${styles.reveal}`}
          onSubmit={(e) => {
            e.preventDefault();
            if (imapEmail && imapPass) task.start();
          }}
        >
          <div>
            <label htmlFor="imap-email" className="label">
              Email address
            </label>
            <input id="imap-email" className="input" type="email" value={imapEmail} onChange={(e) => setImapEmail(e.target.value)} />
          </div>
          <div>
            <label htmlFor="imap-pass" className="label">
              App password
            </label>
            <input id="imap-pass" className="input" type="password" value={imapPass} onChange={(e) => setImapPass(e.target.value)} />
          </div>
          <button type="submit" className="btn btn-primary" disabled={!imapEmail || !imapPass || task.state === "working"}>
            {task.state === "working" ? "Connecting…" : "Connect"}
          </button>
        </form>
      ) : null}

      <button type="button" className="btn btn-primary btn-lg btn-block" disabled={task.state !== "done"} onClick={onNext}>
        Continue
      </button>
    </>
  );
}

/* ---------- 4. Bank ---------- */

const banks = ["CaixaBank", "BBVA", "Santander", "Sabadell", "ING", "Revolut Business"];

function BankStep({ headingRef, onNext }: StepProps) {
  const [bank, setBank] = useState<string | null>(null);
  const task = useFakeTask(1300);
  return (
    <>
      <StepHead
        headingRef={headingRef}
        title="Connect your bank"
        text="Read-only, through Open Banking. I can see transactions. I can never move money."
      />
      <div className={styles.bankGrid}>
        {banks.map((b) => (
          <button
            key={b}
            type="button"
            className="choice"
            data-selected={bank === b}
            disabled={task.state === "working"}
            onClick={() => {
              setBank(b);
              task.start();
            }}
          >
            <Icon name="bank" size={18} style={{ color: "var(--text-2)" }} />
            {b}
          </button>
        ))}
      </div>
      <div aria-live="polite" className={styles.bankStatus}>
        {bank && task.state === "working" ? <p className="muted">Connecting to {bank}…</p> : null}
        {bank && task.state === "done" ? (
          <p className={styles.connectedLine}>
            <Icon name="check" size={18} strokeWidth={2.2} />
            {bank} connected. I found 2 accounts.
          </p>
        ) : null}
      </div>
      <button type="button" className="btn btn-primary btn-lg btn-block" disabled={task.state !== "done"} onClick={onNext}>
        Continue
      </button>
    </>
  );
}

/* ---------- 5. Accountant ---------- */

const software = ["A3", "Sage", "Holded", "Contasol", "Other"];

function AccountantStep({ headingRef, onNext }: StepProps) {
  const [mode, setMode] = useState<"invite" | "software" | "none" | null>(null);
  const [email, setEmail] = useState("");
  const [invited, setInvited] = useState(false);
  const [tool, setTool] = useState<string | null>(null);
  const validEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
  const ready = mode === "none" || (mode === "invite" && invited) || (mode === "software" && tool !== null);

  return (
    <>
      <StepHead
        headingRef={headingRef}
        title="Your accountant"
        text="I prepare everything they need and answer their questions, so you don’t have to."
      />
      <div className="stack-1">
        <button type="button" className="choice" data-selected={mode === "invite"} onClick={() => setMode("invite")}>
          <span className="radio-mark" aria-hidden="true" />
          Invite them by email
        </button>
        {mode === "invite" ? (
          <form
            className={`${styles.inline} ${styles.reveal} ${styles.nested}`}
            onSubmit={(e) => {
              e.preventDefault();
              if (validEmail) setInvited(true);
            }}
          >
            <label htmlFor="acc-email" className="visually-hidden">
              Accountant’s email
            </label>
            <input
              id="acc-email"
              className="input"
              type="email"
              placeholder="marc@asesoriavidal.es"
              value={email}
              onChange={(e) => {
                setEmail(e.target.value);
                setInvited(false);
              }}
            />
            <button type="submit" className="btn btn-secondary" disabled={!validEmail || invited}>
              {invited ? "Invited" : "Invite"}
            </button>
          </form>
        ) : null}

        <button type="button" className="choice" data-selected={mode === "software"} onClick={() => setMode("software")}>
          <span className="radio-mark" aria-hidden="true" />
          Tell me which software they use
        </button>
        {mode === "software" ? (
          <div className={`${styles.chips} ${styles.reveal} ${styles.nested}`} role="radiogroup" aria-label="Accounting software">
            {software.map((s) => (
              <button key={s} type="button" role="radio" aria-checked={tool === s} className={styles.chipChoice} onClick={() => setTool(s)}>
                {s}
              </button>
            ))}
          </div>
        ) : null}

        <button type="button" className="choice" data-selected={mode === "none"} onClick={() => setMode("none")}>
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

/* ---------- 6. Start ---------- */

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
