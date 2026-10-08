import type { ReactNode } from "react";
import type { Tone } from "@/lib/types";
import { Icon } from "./Icon";

export function Dot({ tone }: { tone: Tone }) {
  return <span className={`dot dot-${tone}`} aria-hidden="true" />;
}

export function Status({ tone, label }: { tone: Tone; label: string }) {
  return (
    <span className={`status status-${tone}`}>
      <Dot tone={tone} />
      {label}
    </span>
  );
}

export function Progress({ value, tone, label }: { value: number; tone?: Tone; label: string }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div
      className={`progress${tone === "good" ? " progress-good" : tone === "attention" ? " progress-attention" : ""}`}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={pct}
      aria-label={label}
    >
      <span style={{ width: `${pct}%` }} />
    </div>
  );
}

export function Disclosure({
  summary,
  children,
  className,
  id,
}: {
  summary: ReactNode;
  children: ReactNode;
  className?: string;
  id?: string;
}) {
  return (
    <details className={`disclosure${className ? ` ${className}` : ""}`} id={id}>
      <summary>
        {summary}
        <Icon name="chevronDown" size={16} />
      </summary>
      <div className="disclosure-body">{children}</div>
    </details>
  );
}

export function CheckList({ items }: { items: string[] }) {
  return (
    <ul className="checks">
      {items.map((r) => (
        <li key={r}>
          <Icon name="check" size={16} strokeWidth={2} />
          <span>{r}</span>
        </li>
      ))}
    </ul>
  );
}

export function Bullets({ items }: { items: string[] }) {
  return (
    <ul className="bullets">
      {items.map((r) => (
        <li key={r}>{r}</li>
      ))}
    </ul>
  );
}
