"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { Icon } from "@/components/Icon";
import { safeNext, signUp } from "@/lib/account";
import { pendingInvitation } from "@/lib/invite";
import { checkNif } from "@/lib/nif";
import styles from "./auth.module.css";
import { Field, FormAlert, PasswordRule, emailError } from "./Field";

type Key = "name" | "email" | "password" | "companyName" | "taxId";
type Values = Record<Key, string>;
type Errors = Partial<Record<Key, string | null>>;

const ORDER: Key[] = ["name", "email", "password", "companyName", "taxId"];
const IDS: Record<Key, string> = {
  name: "signup-name",
  email: "signup-email",
  password: "signup-password",
  companyName: "signup-company",
  taxId: "signup-taxid",
};
export const MIN_PASSWORD = 10;

/** Plain-language problems with the form, field by field. The tax number is optional. */
export function validateSignUp(v: Values): Errors {
  const tax = v.taxId.trim() ? checkNif(v.taxId) : null;
  return {
    name: v.name.trim() ? null : "Enter your name.",
    email: emailError(v.email),
    password:
      v.password.length === 0
        ? "Choose a password."
        : v.password.length < MIN_PASSWORD
          ? `Use at least ${MIN_PASSWORD} characters. This one has ${v.password.length}.`
          : null,
    companyName: v.companyName.trim() ? null : "Enter your company’s name.",
    taxId: tax && !tax.valid ? tax.message : null,
  };
}

export function SignUpForm() {
  const router = useRouter();
  const params = useSearchParams();
  const hasNext = params.get("next");
  const signinHref = hasNext ? `/signin?next=${encodeURIComponent(safeNext(hasNext))}` : "/signin";

  const [values, setValues] = useState<Values>({ name: "", email: "", password: "", companyName: "", taxId: "" });
  const [errors, setErrors] = useState<Errors>({});
  const [touched, setTouched] = useState<Partial<Record<Key, boolean>>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const focus = (key: Key) => document.getElementById(IDS[key])?.focus();

  const set = (key: Key) => (e: React.ChangeEvent<HTMLInputElement>) => {
    const next = { ...values, [key]: e.target.value };
    setValues(next);
    // Once a field has been checked, keep its message in step with what is typed.
    if (touched[key] || errors[key]) setErrors((x) => ({ ...x, [key]: validateSignUp(next)[key] }));
  };

  // The tax number is checked as soon as the owner leaves the field (inline format check).
  const blur = (key: Key) => () => {
    setTouched((t) => ({ ...t, [key]: true }));
    if (key === "taxId" || values[key]) setErrors((x) => ({ ...x, [key]: validateSignUp(values)[key] }));
  };

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    const found = validateSignUp(values);
    setErrors(found);
    setTouched({ name: true, email: true, password: true, companyName: true, taxId: true });
    setFormError(null);
    const first = ORDER.find((k) => found[k]);
    if (first) return focus(first);

    setBusy(true);
    const tax = values.taxId.trim() ? checkNif(values.taxId) : null;
    const result = await signUp({ ...values, taxId: tax?.valid ? tax.normalized : undefined });
    if (result.ok) {
      // Invited by their accountant (§29): accept first, then set up.
      router.replace(pendingInvitation() ? "/invite" : "/onboarding");
      return;
    }
    setBusy(false);
    const field = result.field as Key | undefined;
    if (field && ORDER.includes(field)) {
      setErrors((x) => ({ ...x, [field]: result.message }));
      focus(field);
    } else {
      setFormError(result.message);
      focus("email");
    }
  };

  const describe = formError ? "signup-error" : undefined;

  return (
    <div className={styles.auth}>
      <header className={styles.head}>
        <h1 className="h1">Create your account</h1>
        <p className="lead">Connect your business once. I handle the rest.</p>
      </header>

      <form className={styles.form} onSubmit={submit} noValidate>
        <FormAlert id="signup-error" message={formError} />
        <div className={styles.pair}>
          <Field
            id={IDS.name}
            label="Your name"
            name="name"
            autoComplete="name"
            required
            value={values.name}
            error={errors.name}
            describedBy={describe}
            onChange={set("name")}
            onBlur={blur("name")}
          />
          <Field
            id={IDS.email}
            label="Work email"
            type="email"
            name="email"
            autoComplete="email"
            inputMode="email"
            autoCapitalize="none"
            spellCheck={false}
            required
            value={values.email}
            error={errors.email}
            describedBy={describe}
            onChange={set("email")}
            onBlur={blur("email")}
          />
          <Field
            id={IDS.password}
            label="Password"
            name="new-password"
            autoComplete="new-password"
            required
            minLength={MIN_PASSWORD}
            revealable
            value={values.password}
            error={errors.password}
            describedBy={[errors.password ? null : "signup-password-rule", describe].filter(Boolean).join(" ") || undefined}
            onChange={set("password")}
            onBlur={blur("password")}
          >
            {/* The rule, until there is an error: the error then says the same thing with the count. */}
            {errors.password ? null : <PasswordRule id="signup-password-rule" met={values.password.length >= MIN_PASSWORD} />}
          </Field>
        </div>

        <div className={styles.pair}>
          <Field
            id={IDS.companyName}
            label="Company name"
            name="organization"
            autoComplete="organization"
            required
            value={values.companyName}
            error={errors.companyName}
            describedBy={describe}
            onChange={set("companyName")}
            onBlur={blur("companyName")}
          />
          <Field
            id={IDS.taxId}
            label="Company tax number (NIF)"
            optional
            name="taxId"
            autoComplete="off"
            inputMode="numeric"
            spellCheck={false}
            placeholder="123 456 789"
            hint="9 digits. I use it to find your invoices and fill in the rest."
            value={values.taxId}
            error={errors.taxId}
            describedBy={describe}
            onChange={set("taxId")}
            onBlur={blur("taxId")}
          />
        </div>

        <button type="submit" className="btn btn-primary btn-lg btn-block" disabled={busy} aria-disabled={busy}>
          {busy ? "Creating your account…" : "Create account"}
        </button>
        <p className={styles.reassure}>
          <Icon name="shield" size={18} />
          <span>I never move money, and I never send anything without asking you first.</span>
        </p>
      </form>

      <div className={styles.aside}>
        <p>
          Already have an account?{" "}
          <Link className="link" href={signinHref}>
            Sign in
          </Link>
        </p>
      </div>
    </div>
  );
}
