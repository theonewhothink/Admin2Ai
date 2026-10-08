"use client";

import { useState } from "react";
import detail from "@/components/detail/detail.module.css";
import { Result } from "@/components/detail/parts";
import { send, useApi } from "@/components/detail/useApi";
import { HistoryChoice } from "@/components/settings/HistoryChoice";
import { liveData } from "@/lib/api";
import type { HistoryChoice as Choice, ReadingData } from "@/lib/types";

/**
 * How I read your email and bank (spec §6, §8): how far back the first read of a new connection goes (the last
 * 90 days or the last 12 months; choosing 12 months also reads the older months of what is already connected),
 * and whether I also look in spam for invoices (off until the owner switches it on; never the trash).
 */
export function ReadingSettings() {
  const { data, error, reload } = useApi<ReadingData>(liveData ? "/api/settings/reading" : null);
  const [saved, setSaved] = useState<ReadingData | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  if (!liveData) return null;
  // The engine answers a change with the whole view; show that until the next read.
  const view = saved ?? data;

  const change = async (body: { history?: Choice; lookInSpam?: boolean }) => {
    setBusy(true);
    setResult(null);
    const r = await send<ReadingData>("/api/settings/reading", body);
    setBusy(false);
    setResult({ ok: r.ok, text: r.message });
    if (r.ok && typeof r.body.lookInSpam === "boolean") setSaved(r.body);
    else reload();
  };

  return (
    <section aria-labelledby="reading-h" id="reading">
      <div className="section-head">
        <h2 id="reading-h" className="h2">
          How I read your email and bank
        </h2>
      </div>
      {!view ? (
        <p className="card card-pad muted" role="status">
          {error ?? "One moment…"}
        </p>
      ) : (
        <div className="stack-2">
          <div className="card card-pad stack-2">
            <HistoryChoice
              value={view.history}
              disabled={busy}
              legend={view.historyLabel}
              name="settings-history"
              onChange={(history) => void change({ history })}
            />
            <p className="meta">{view.historyDetail}</p>
          </div>
          <ul className="card list">
            <li>
              <div className={detail.switchRow}>
                <span className={detail.switchText}>
                  <span className={detail.switchLabel} id="reading-spam">
                    {view.spamLabel}
                  </span>
                  <span className="meta" id="reading-spam-detail">
                    {view.spamDetail}
                  </span>
                </span>
                <button
                  type="button"
                  role="switch"
                  className={detail.switch}
                  aria-checked={view.lookInSpam}
                  aria-labelledby="reading-spam"
                  aria-describedby="reading-spam-detail"
                  disabled={busy}
                  onClick={() => void change({ lookInSpam: !view.lookInSpam })}
                />
              </div>
            </li>
          </ul>
          <Result result={result} />
          {view.reading.length > 0 ? (
            <p className="meta" role="status">
              Reading the older months of {view.reading.join(", ")} now.
            </p>
          ) : null}
        </div>
      )}
    </section>
  );
}
