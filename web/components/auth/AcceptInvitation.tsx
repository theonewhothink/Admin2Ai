"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Icon } from "@/components/Icon";
import { getSession } from "@/lib/account";
import { acceptInvitation } from "@/lib/api";
import { forgetInvitation, takeInvitationToken } from "@/lib/invite";
import type { Session } from "@/lib/owner";
import styles from "./auth.module.css";

type Stage = { kind: "loading" } | { kind: "ready"; token: string | null; session: Session | null };

/**
 * "Your accountant has enabled Back Office for you." (§29). The invited owner signs up or signs in with
 * the address the invitation went to, then accepts: their accountant can then see their business.
 */
export function AcceptInvitation() {
  const [stage, setStage] = useState<Stage>({ kind: "loading" });
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  useEffect(() => {
    let live = true;
    const token = takeInvitationToken();
    void getSession().then((session) => {
      if (live) setStage({ kind: "ready", token, session });
    });
    return () => {
      live = false;
    };
  }, []);

  const accept = async (token: string) => {
    setBusy(true);
    const out = await acceptInvitation(token);
    setBusy(false);
    setResult({ ok: out.ok, text: out.message ?? (out.ok ? "Done." : "That did not work. Try again.") });
    if (out.ok) forgetInvitation();
  };

  const token = stage.kind === "ready" ? stage.token : null;
  const session = stage.kind === "ready" ? stage.session : null;

  return (
    <section className={styles.auth} aria-labelledby="invite-h">
      <header className={styles.head}>
        <h1 id="invite-h" className="h1">
          Your accountant has enabled Back Office for you.
        </h1>
        <p className="lead">
          Connect your email and your bank once. Back Office collects your invoices and receipts, matches them to
          your payments and sends the month to your accountant.
        </p>
      </header>
      {stage.kind === "loading" ? <p className="muted">One moment…</p> : null}
      {stage.kind === "ready" && !token ? (
        <p className="risk-text" role="alert">
          This invitation link is not valid. Ask your accountant to send a new one.
        </p>
      ) : null}
      {token && !session ? (
        <div className="stack-2">
          <p>Use the email address the invitation was sent to.</p>
          <div className={styles.aside}>
            <Link href="/signup?next=%2Finvite" className="btn btn-primary">
              Create my account
            </Link>
            <Link href="/signin?next=%2Finvite" className="btn btn-secondary">
              I already have an account
            </Link>
          </div>
        </div>
      ) : null}
      {token && session && !result?.ok ? (
        <div className="stack-2">
          <p>
            Signed in as <strong>{session.user.email}</strong>. Accepting lets your accountant see this business’s
            documents and payments.
          </p>
          <div>
            <button type="button" className="btn btn-primary" disabled={busy} onClick={() => accept(token)}>
              {busy ? "One moment…" : "Accept"}
            </button>
          </div>
        </div>
      ) : null}
      <div aria-live="polite">
        {result ? (
          <p className={result.ok ? "good-text" : "risk-text"} role={result.ok ? "status" : "alert"}>
            {result.ok ? <Icon name="check" size={18} strokeWidth={2.2} /> : null} {result.text}
          </p>
        ) : null}
        {result?.ok ? (
          <p>
            <Link href="/" className="btn btn-secondary">
              Go to Home
            </Link>
          </p>
        ) : null}
      </div>
    </section>
  );
}
