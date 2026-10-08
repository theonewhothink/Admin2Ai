"use client";

import { useState } from "react";
import detail from "@/components/detail/detail.module.css";
import { Result } from "@/components/detail/parts";
import { send, useApi } from "@/components/detail/useApi";
import { Disclosure } from "@/components/ui";
import { liveData } from "@/lib/api";
import type { AutomationData, AutomationItem } from "@/lib/types";

/**
 * What I may do on my own (spec §25, "automatic if authorized"): ask suppliers for missing invoices,
 * answer the accountant's routine questions, send each month to the accountant. Moving money, tax filings,
 * bank details, accepting terms and deleting originals always wait for a yes, every time.
 */
export function AutomationSettings() {
  const { data, error, reload } = useApi<AutomationData>(liveData ? "/api/settings/automation" : null);
  const [saved, setSaved] = useState<AutomationData | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  if (!liveData) return null;
  // The engine answers a change with the whole view; show that until the next read.
  const view = saved ?? data;

  const change = async (item: AutomationItem, on: boolean, companyId?: string) => {
    setBusy(companyId ? `${item.id}:${companyId}` : item.id);
    setResult(null);
    const r = await send<AutomationData>("/api/settings/automation", { [item.id]: on, ...(companyId ? { companyId } : {}) });
    setBusy(null);
    setResult({ ok: r.ok, text: r.message });
    if (r.ok && Array.isArray(r.body.items)) setSaved(r.body);
    else reload();
  };

  return (
    <section aria-labelledby="auto-h" id="automation">
      <div className="section-head">
        <h2 id="auto-h" className="h2">
          What I do on my own
        </h2>
      </div>
      {!view ? (
        <p className="card card-pad muted" role="status">
          {error ?? "One moment…"}
        </p>
      ) : (
        <div className="stack-2">
          <ul className="card list">
            {view.items.map((item) => {
              const some = !item.on && item.onFor.length > 0;
              const names = item.companies.filter((c) => c.on).map((c) => c.companyName);
              return (
                <li key={item.id}>
                  <div className={detail.switchRow}>
                    <span className={detail.switchText}>
                      <span className={detail.switchLabel} id={`auto-${item.id}`}>
                        {item.label}
                      </span>
                      <span className="meta" id={`auto-${item.id}-detail`}>
                        {item.detail}
                        {some ? ` On for ${names.join(", ")}.` : ""}
                      </span>
                    </span>
                    <button
                      type="button"
                      role="switch"
                      className={detail.switch}
                      aria-checked={item.on}
                      aria-labelledby={`auto-${item.id}`}
                      aria-describedby={`auto-${item.id}-detail`}
                      disabled={busy !== null}
                      onClick={() => void change(item, !item.on)}
                    />
                  </div>
                  {item.companies.length > 1 ? (
                    <div className={detail.perCompany}>
                      <Disclosure summary="For each company">
                        <div className="stack-1">
                          {item.companies.map((c) => (
                            <label key={c.companyId} className="checkbox">
                              <input
                                type="checkbox"
                                checked={c.on}
                                disabled={busy !== null}
                                onChange={(e) => void change(item, e.target.checked, c.companyId)}
                              />
                              <span>{c.companyName}</span>
                            </label>
                          ))}
                        </div>
                      </Disclosure>
                    </div>
                  ) : null}
                </li>
              );
            })}
          </ul>
          <Result result={result} />
          <p className="meta">
            {view.summary} {view.never}
          </p>
        </div>
      )}
    </section>
  );
}
