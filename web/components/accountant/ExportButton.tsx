"use client";

import { useState } from "react";
import { Icon } from "@/components/Icon";
import { call, saveFile } from "@/lib/api";

interface ExportReply {
  filename?: string;
  contentType?: string;
  data?: string;
  count?: number;
  message?: string;
}

/**
 * The client's documents for the month as one ZIP (originals, ledger.csv and manifest.json), built by the
 * engine or the backend and saved in the browser: from the client's `links.export` (`path`), else
 * POST /api/documents/export for the company and period. What is shown is what came back (the file and
 * its document count) or its reason for failing; nothing is assumed.
 */
export function ExportButton({
  label,
  software,
  companyId,
  period,
  path,
}: {
  label: string;
  software: string;
  companyId: string;
  period?: { from: string; to: string };
  path?: string;
}) {
  const [state, setState] = useState<"idle" | "working">("idle");
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);

  const run = async () => {
    setState("working");
    setResult(null);
    const r = path
      ? await call<ExportReply>("GET", path)
      : await call<ExportReply>("POST", "/api/documents/export", { company: companyId, ...(period ?? {}) });
    setState("idle");
    const { filename, contentType, data, count } = r.body;
    if (!r.ok || !filename || !data) {
      setResult({ ok: false, text: r.body.message ?? "I couldn’t prepare the export. Try again." });
      return;
    }
    saveFile({ filename, contentType: contentType ?? "application/zip", data });
    const docs = count === 1 ? "1 document" : `${count ?? 0} documents`;
    setResult({ ok: true, text: `Downloaded ${filename}: ${docs}, with the ledger. Import it into ${software}.` });
  };

  return (
    <div className="stack-1">
      <div>
        <button type="button" className="btn btn-secondary" disabled={state === "working"} onClick={() => void run()}>
          <Icon name="export" size={18} />
          {state === "working" ? "Preparing…" : label}
        </button>
      </div>
      <div aria-live="polite">
        {result ? (
          <p
            className={result.ok ? "good-text" : "meta"}
            role={result.ok ? "status" : "alert"}
            style={result.ok ? { display: "inline-flex", alignItems: "center", gap: 8, fontWeight: 550 } : undefined}
          >
            {result.ok ? <Icon name="check" size={18} strokeWidth={2.2} /> : null}
            {result.text}
          </p>
        ) : null}
      </div>
    </div>
  );
}
