"use client";

/**
 * Settings → Account (production only, spec §52): who is signed in, download
 * everything, delete the account. Deleting asks the owner to type DELETE and
 * their password; the API checks the password again.
 */
import { useEffect, useRef, useState } from "react";
import { Icon } from "@/components/Icon";
import { useSession } from "@/components/session/context";
import { deleteAccount, exportAccount, signInPage } from "@/lib/account";
import styles from "./auth.module.css";
import { Field, FormAlert } from "./Field";

type ExportState = { phase: "idle" } | { phase: "working" } | { phase: "done" } | { phase: "failed"; message: string };

export function AccountSection() {
  const session = useSession();
  const [exporting, setExporting] = useState<ExportState>({ phase: "idle" });
  const [confirming, setConfirming] = useState(false);

  const download = async () => {
    setExporting({ phase: "working" });
    const problem = await exportAccount();
    setExporting(problem ? { phase: "failed", message: problem } : { phase: "done" });
  };

  return (
    <section aria-labelledby="account-h">
      <div className="section-head">
        <h2 id="account-h" className="h2">
          Account
        </h2>
      </div>
      <div className="card">
        <div className={styles.accountRow}>
          <Icon name="user" size={20} className={styles.accountIcon} />
          <div className={styles.accountText}>
            <span className={styles.accountTitle}>Signed in as</span>
            <span className="meta">{session ? session.user.email : "…"}</span>
          </div>
        </div>

        <div className={styles.accountRow}>
          <Icon name="download" size={20} className={styles.accountIcon} />
          <div className={styles.accountText}>
            <span className={styles.accountTitle}>Download my data</span>
            <span className="meta">Every document, what I found in it, and each step I took, in one file.</span>
            <span className={styles.status} role="status" aria-live="polite">
              {exporting.phase === "working"
                ? "Preparing your file. Large accounts can take a minute."
                : exporting.phase === "done"
                  ? "Your file is downloading."
                  : exporting.phase === "failed"
                    ? exporting.message
                    : ""}
            </span>
          </div>
          <button type="button" className="btn btn-secondary" onClick={() => void download()} disabled={exporting.phase === "working"}>
            {exporting.phase === "working" ? "Preparing…" : "Download"}
          </button>
        </div>

        <DeleteAccount confirming={confirming} setConfirming={setConfirming} />
      </div>
    </section>
  );
}

function DeleteAccount({ confirming, setConfirming }: { confirming: boolean; setConfirming: (v: boolean) => void }) {
  const [typed, setTyped] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<{ typed?: string | null; password?: string | null }>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const openRef = useRef<HTMLButtonElement>(null);
  const typedRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const wasOpen = useRef(false);

  // Focus follows the panel: into it when it opens, back to the button when it closes.
  useEffect(() => {
    if (confirming) typedRef.current?.focus();
    else if (wasOpen.current) openRef.current?.focus();
    wasOpen.current = confirming;
  }, [confirming]);

  const cancel = () => {
    setTyped("");
    setPassword("");
    setErrors({});
    setFormError(null);
    setConfirming(false);
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const found = {
      typed: typed.trim() === "DELETE" ? null : "Type DELETE in capital letters to confirm.",
      password: password ? null : "Enter your password.",
    };
    setErrors(found);
    setFormError(null);
    if (found.typed) return typedRef.current?.focus();
    if (found.password) return passwordRef.current?.focus();

    setBusy(true);
    const result = await deleteAccount(password);
    if (result.ok) {
      window.location.assign(signInPage("?deleted=1"));
      return;
    }
    setBusy(false);
    if (result.field === "password") {
      setErrors({ password: result.message });
      passwordRef.current?.focus();
      passwordRef.current?.select();
    } else {
      setFormError(result.message);
    }
  };

  return (
    <>
      <div className={styles.accountRow}>
        <Icon name="trash" size={20} className={styles.accountIcon} />
        <div className={styles.accountText}>
          <span className={styles.accountTitle}>Delete my account</span>
          <span className="meta">Removes your businesses, documents and history from Admin2Ai.</span>
        </div>
        {confirming ? null : (
          <button
            ref={openRef}
            type="button"
            className="btn btn-secondary"
            aria-expanded={false}
            aria-controls="delete-account"
            onClick={() => setConfirming(true)}
          >
            Delete account…
          </button>
        )}
      </div>
      {confirming ? (
        <form id="delete-account" className={styles.danger} onSubmit={submit} noValidate aria-labelledby="delete-h">
          <div className="stack-1">
            <h3 id="delete-h" className="h3">
              Delete your account for good?
            </h3>
            <p className="muted">
              This deletes every business, document and answer you gave me, and signs you out on every device. It can’t be
              undone. If you want a copy, download your data first.
            </p>
          </div>
          <FormAlert id="delete-error" message={formError} />
          <Field
            id="delete-confirm"
            label="Type DELETE to confirm"
            name="confirm"
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
            value={typed}
            inputRef={typedRef}
            error={errors.typed}
            describedBy={formError ? "delete-error" : undefined}
            onChange={(e) => setTyped(e.target.value)}
          />
          <Field
            id="delete-password"
            label="Your password"
            name="password"
            autoComplete="current-password"
            revealable
            value={password}
            inputRef={passwordRef}
            error={errors.password}
            describedBy={formError ? "delete-error" : undefined}
            onChange={(e) => setPassword(e.target.value)}
          />
          <div className={styles.dangerActions}>
            <button type="submit" className="btn btn-risk" disabled={busy}>
              {busy ? "Deleting…" : "Delete my account"}
            </button>
            <button type="button" className="btn btn-quiet" onClick={cancel} disabled={busy}>
              Keep my account
            </button>
          </div>
        </form>
      ) : null}
    </>
  );
}
