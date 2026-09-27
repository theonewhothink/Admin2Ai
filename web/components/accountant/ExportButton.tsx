"use client";

import { useState } from "react";
import { Icon } from "@/components/Icon";

export function ExportButton({ label, software, count }: { label: string; software: string; count: number }) {
  const [state, setState] = useState<"idle" | "working" | "done">("idle");
  if (state === "done") {
    return (
      <p className="good-text" role="status" style={{ display: "inline-flex", alignItems: "center", gap: 8, fontWeight: 550 }}>
        <Icon name="check" size={18} strokeWidth={2.2} />
        Sent to {software}. {count} transactions.
      </p>
    );
  }
  return (
    <button
      type="button"
      className="btn btn-secondary"
      disabled={state === "working"}
      onClick={() => {
        setState("working");
        setTimeout(() => setState("done"), 900);
      }}
    >
      <Icon name="export" size={18} />
      {state === "working" ? "Exporting…" : label}
    </button>
  );
}
