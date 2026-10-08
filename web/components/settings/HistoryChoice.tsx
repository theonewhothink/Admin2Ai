"use client";

import type { HistoryChoice as Choice } from "@/lib/types";

const OPTIONS: { id: Choice; label: string; sub: string }[] = [
  { id: "90d", label: "Last 90 days", sub: "Quick to start. Enough for most businesses." },
  { id: "12m", label: "Last 12 months", sub: "Takes a little longer. I learn your whole year." },
];

/**
 * How far back I read a new mailbox or bank the first time (spec §6): the last 90 days (the default) or the
 * last 12 months. Used in onboarding and in Settings; the caller saves the choice (POST /api/settings/reading).
 */
export function HistoryChoice({
  value,
  onChange,
  disabled = false,
  legend = "How far back should I read?",
  name = "history",
}: {
  value: Choice;
  onChange: (choice: Choice) => void;
  disabled?: boolean;
  legend?: string;
  name?: string;
}) {
  return (
    <fieldset style={{ border: "none", padding: 0, margin: 0, minWidth: 0, display: "grid", gap: 8 }}>
      <legend className="label" style={{ marginBottom: 6 }}>
        {legend}
      </legend>
      {OPTIONS.map((o) => (
        <label key={o.id} className="choice">
          <input
            type="radio"
            name={name}
            value={o.id}
            checked={value === o.id}
            disabled={disabled}
            onChange={() => onChange(o.id)}
          />
          <span className="radio-mark" aria-hidden="true" />
          <span style={{ display: "grid", gap: 2 }}>
            <span>{o.label}</span>
            <span className="meta">{o.sub}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}
