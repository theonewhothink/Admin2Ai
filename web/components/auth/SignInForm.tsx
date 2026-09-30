"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Icon } from "@/components/Icon";
import { getSession, safeNext, signIn } from "@/lib/account";
import { SUPPORT_EMAIL } from "@/lib/mode";
import styles from "./auth.module.css";
import { EMAIL_SHAPE, Field, FormAlert, emailError } from "./Field";

export function SignInForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const deleted = params.get("deleted") === "1";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<{ email?: string | null; password?: string | null }>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const emailRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);

  // Already signed in (another tab, or the back button): go straight on.
  useEffect(() => {
    let live = true;
    void getSession().then((session) => {
      if (live && session) router.replace(next);
    });
    return () => {
      live = false;
    };
  }, [router, next]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const found = { email: emailError(email), password: password ? null : "Enter your password." };
    setErrors(found);
    setFormError(null);
    if (found.email) return emailRef.current?.focus();
    if (found.password) return passwordRef.current?.focus();

    setBusy(true);
    const result = await signIn(email, password);
    if (result.ok) {
      router.replace(next);
      return;
    }
    setBusy(false);
    setFormError(result.message);
    passwordRef.current?.focus();
    passwordRef.current?.select();
  };

  const signupHref = next !== "/" ? `/signup?next=${encodeURIComponent(next)}` : "/signup";

  return (
    <div className={styles.auth}>
      <header className={styles.head}>
        <h1 className="h1">Sign in</h1>
        <p className="lead">Welcome back. I kept going while you were away.</p>
      </header>

      {deleted ? (
        <p className={styles.note} role="status">
          <Icon name="checkCircle" size={18} />
          <span>Your account and its data are being deleted. Thank you for trying Admin2Ai.</span>
        </p>
      ) : null}

      <form className={styles.form} onSubmit={submit} noValidate>
        <FormAlert id="signin-error" message={formError} />
        <Field
          id="signin-email"
          label="Email"
          type="email"
          name="email"
          autoComplete="username"
          inputMode="email"
          autoCapitalize="none"
          spellCheck={false}
          required
          value={email}
          inputRef={emailRef}
          error={errors.email}
          describedBy={formError ? "signin-error" : undefined}
          onChange={(e) => {
            setEmail(e.target.value);
            if (errors.email && EMAIL_SHAPE.test(e.target.value.trim())) setErrors((x) => ({ ...x, email: null }));
          }}
        />
        <Field
          id="signin-password"
          label="Password"
          name="password"
          autoComplete="current-password"
          required
          revealable
          value={password}
          inputRef={passwordRef}
          error={errors.password}
          describedBy={formError ? "signin-error" : undefined}
          onChange={(e) => {
            setPassword(e.target.value);
            if (errors.password && e.target.value) setErrors((x) => ({ ...x, password: null }));
          }}
        />
        <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={busy} aria-disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>

      <div className={styles.aside}>
        <details className={`disclosure ${styles.forgot}`}>
          <summary>
            Forgot password?
            <Icon name="chevronDown" size={16} />
          </summary>
          <p className={`disclosure-body ${styles.forgotBody}`}>
            {SUPPORT_EMAIL ? (
              <>
                Ask support at{" "}
                <a className="link" href={`mailto:${SUPPORT_EMAIL}?subject=${encodeURIComponent("Reset my password")}`}>
                  {SUPPORT_EMAIL}
                </a>{" "}
                to reset it. Write from the email you sign in with.
              </>
            ) : (
              <>Ask support to reset it. Write from the email you sign in with.</>
            )}
          </p>
        </details>
        <p>
          New here?{" "}
          <Link className="link" href={signupHref}>
            Create an account
          </Link>
        </p>
      </div>
    </div>
  );
}
