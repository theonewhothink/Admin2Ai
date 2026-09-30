"use client";

import { useState, type InputHTMLAttributes, type ReactNode, type Ref } from "react";
import { Icon } from "@/components/Icon";
import styles from "./auth.module.css";

type InputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "id" | "className" | "aria-describedby" | "aria-invalid">;

/**
 * A labelled input with its hint and error tied to it (aria-describedby), so
 * screen readers read the rule and the problem with the field. The error
 * replaces nothing: the hint stays, the error is added under it.
 */
export function Field({
  id,
  label,
  hint,
  error,
  optional,
  describedBy,
  inputRef,
  revealable,
  children,
  ...input
}: InputProps & {
  id: string;
  label: string;
  hint?: ReactNode;
  error?: string | null;
  optional?: boolean;
  /** Extra ids to describe the input (e.g. a form-level message). */
  describedBy?: string;
  inputRef?: Ref<HTMLInputElement>;
  /** Password fields get a Show / Hide button. */
  revealable?: boolean;
  /** Rendered under the input, before the error (e.g. a live rule). */
  children?: ReactNode;
}) {
  const [shown, setShown] = useState(false);
  const hintId = hint ? `${id}-hint` : null;
  const errorId = error ? `${id}-error` : null;
  const described = [hintId, errorId, describedBy].filter(Boolean).join(" ") || undefined;
  const type = revealable ? (shown ? "text" : "password") : input.type;
  return (
    <div className={styles.field}>
      <div className={styles.labelRow}>
        <label htmlFor={id} className="label">
          {label}
        </label>
        {optional ? <span className={styles.optional}>Optional</span> : null}
      </div>
      <div className={`${styles.control} ${revealable ? styles.withToggle : ""}`}>
        <input
          {...input}
          ref={inputRef}
          id={id}
          type={type}
          className="input"
          aria-describedby={described}
          aria-invalid={error ? true : undefined}
        />
        {revealable ? (
          <button
            type="button"
            className={styles.toggle}
            aria-controls={id}
            aria-pressed={shown}
            onClick={() => setShown((v) => !v)}
          >
            {shown ? "Hide" : "Show"}
            <span className="visually-hidden"> password</span>
          </button>
        ) : null}
      </div>
      {hint ? (
        <p id={hintId ?? undefined} className={styles.hint}>
          {hint}
        </p>
      ) : null}
      {children}
      {error ? (
        <p id={errorId ?? undefined} className={styles.error}>
          <Icon name="alert" size={16} strokeWidth={1.8} />
          {error}
        </p>
      ) : null}
    </div>
  );
}

/** The 10-character rule, shown from the start and ticked when met. */
export function PasswordRule({ id, met }: { id: string; met: boolean }) {
  return (
    <p id={id} className={styles.rule} data-met={met}>
      <span className={styles.ruleMark} aria-hidden="true">
        {met ? <Icon name="check" size={12} strokeWidth={2.6} /> : null}
      </span>
      At least 10 characters
      <span className="visually-hidden">{met ? ", done" : ""}</span>
    </p>
  );
}

/** A plain-language message about the whole form, announced when it appears. */
export function FormAlert({ id, message, alertRef }: { id: string; message: string | null; alertRef?: Ref<HTMLDivElement> }) {
  return (
    <div id={id} ref={alertRef} tabIndex={-1} role="alert" className={message ? styles.alert : styles.alertIdle}>
      {message ? (
        <>
          <Icon name="alert" size={18} strokeWidth={1.8} />
          <span>{message}</span>
        </>
      ) : null}
    </div>
  );
}

export const EMAIL_SHAPE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export function emailError(value: string): string | null {
  if (!value.trim()) return "Enter your email address.";
  if (!EMAIL_SHAPE.test(value.trim())) return "That doesn’t look like an email address. Check it and try again.";
  return null;
}
