"use client";

import { useState } from "react";
import { send } from "@/components/detail/useApi";
import { Icon } from "@/components/Icon";
import type { IdentityCheck as Check } from "@/lib/types";

const CONFIRM_PATH = /^\/api\/companies\/[^/?#]+\/identity$/;

/**
 * The details the EU VAT register gave for a company that differ from what the owner typed: one tap uses
 * them or keeps what was typed (`POST /api/companies/{id}/identity { use }`). Shown only while there is a choice.
 */
export function IdentityCheck({ check }: { check: Check }) {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const path = check.confirmPath;
  if (!path || !CONFIRM_PATH.test(path) || !check.options?.length) return null;

  const choose = async (use: boolean) => {
    setBusy(true);
    setFailed(null);
    const r = await send(path, { use });
    setBusy(false);
    if (r.ok) setDone(r.message);
    else setFailed(r.message);
  };

  if (done) {
    return (
      <p className="notice" role="status">
        <Icon name="check" size={18} />
        <span>{done}</span>
      </p>
    );
  }
  return (
    <section className="card card-pad stack-2" aria-labelledby="identity-h">
      <h2 id="identity-h" className="h3">
        Your company’s details
      </h2>
      <p>{check.message}</p>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {check.options.map((o) => (
          <button
            key={o.id}
            type="button"
            className={`btn ${o.id === "use" ? "btn-primary" : "btn-secondary"}`}
            disabled={busy}
            onClick={() => void choose(o.id === "use")}
          >
            {o.label}
          </button>
        ))}
      </div>
      {failed ? (
        <p role="alert" className="muted">
          {failed}
        </p>
      ) : null}
    </section>
  );
}
