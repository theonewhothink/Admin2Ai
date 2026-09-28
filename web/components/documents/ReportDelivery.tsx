"use client";

import { useEffect, useState } from "react";
import { call } from "@/lib/api";

interface Recipient {
  email: string;
  name: string;
  role: string;
}
interface Settings {
  recipients: Recipient[];
  day: number;
  format: "zip" | "csv";
  includeDocuments: boolean;
  copyOwner: boolean;
  companies: string[];
  companyNames: Record<string, string>;
  ownerEmail: string;
  summary: string;
  message?: string;
}

/** Where the monthly package goes (GET/POST /api/settings/report). */
export function ReportDelivery() {
  const [s, setS] = useState<Settings | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("");
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    void call<Settings>("GET", "/api/settings/report").then((r) => (r.ok ? setS(r.body) : setNote(r.body.message ?? null)));
  }, []);

  async function save(next: Partial<Settings>) {
    if (!s) return;
    const body = { ...s, ...next };
    const r = await call<Settings>("POST", "/api/settings/report", {
      recipients: body.recipients,
      day: body.day,
      format: body.format,
      includeDocuments: body.includeDocuments,
      copyOwner: body.copyOwner,
      companies: body.companies,
    });
    if (r.ok) {
      setS(r.body);
      setNote("Saved.");
    } else setNote(r.body.message ?? "I couldn't save that.");
  }

  return (
    <section aria-labelledby="rep-h" className="stack-2" id="monthly-report">
      <div className="section-head">
        <h2 id="rep-h" className="h2">
          Monthly report
        </h2>
      </div>
      {!s ? (
        <p className="card card-pad meta">{note ?? "Loading…"}</p>
      ) : (
        <div className="card card-pad" style={{ display: "grid", gap: 16 }}>
          <p>{s.summary}</p>
          <ul className="list" style={{ margin: 0 }}>
            {s.recipients.map((r) => (
              <li key={r.email} className="list-row" style={{ padding: "6px 0" }}>
                <span style={{ flex: 1, display: "grid" }}>
                  <span style={{ fontWeight: 600, overflowWrap: "anywhere" }}>{r.email}</span>
                  <span className="meta">{[r.name, r.role].filter(Boolean).join(" · ") || "Recipient"}</span>
                </span>
                <button className="btn btn-quiet" type="button" onClick={() => void save({ recipients: s.recipients.filter((x) => x.email !== r.email) })}>
                  Remove
                </button>
              </li>
            ))}
          </ul>
          <form
            style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "end" }}
            onSubmit={(e) => {
              e.preventDefault();
              void save({ recipients: [...s.recipients, { email, name: "", role }] }).then(() => {
                setEmail("");
                setRole("");
              });
            }}
          >
            <label style={{ display: "grid", gap: 6, flex: 2, minWidth: 200 }}>
              <span className="label">Send to</span>
              <input className="input" type="email" required value={email} placeholder="name@company.pt" onChange={(e) => setEmail(e.target.value)} />
            </label>
            <label style={{ display: "grid", gap: 6, flex: 1, minWidth: 140 }}>
              <span className="label">Role (optional)</span>
              <input className="input" value={role} placeholder="Accountant, partner…" onChange={(e) => setRole(e.target.value)} />
            </label>
            <button className="btn btn-secondary" type="submit">
              Add
            </button>
          </form>
          <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
            <label style={{ display: "grid", gap: 6 }}>
              <span className="label">Send on working day</span>
              <select className="input" value={s.day} onChange={(e) => void save({ day: Number(e.target.value) })}>
                {Array.from({ length: 10 }, (_, i) => i + 1).map((d) => (
                  <option key={d} value={d}>
                    {d}
                  </option>
                ))}
              </select>
            </label>
            <label style={{ display: "grid", gap: 6 }}>
              <span className="label">What to send</span>
              <select className="input" value={s.format} onChange={(e) => void save({ format: e.target.value as Settings["format"] })}>
                <option value="zip">All documents + ledger (ZIP)</option>
                <option value="csv">Ledger only (CSV)</option>
              </select>
            </label>
          </div>
          <fieldset style={{ border: "none", padding: 0, display: "grid", gap: 8 }}>
            <legend className="label" style={{ marginBottom: 6 }}>
              Companies
            </legend>
            {Object.entries(s.companyNames).map(([id, name]) => (
              <label key={id} className="checkbox">
                <input
                  type="checkbox"
                  checked={s.companies.includes(id)}
                  onChange={(e) => void save({ companies: e.target.checked ? [...s.companies, id] : s.companies.filter((c) => c !== id) })}
                />
                {name}
              </label>
            ))}
            <label className="checkbox">
              <input type="checkbox" checked={s.copyOwner} onChange={(e) => void save({ copyOwner: e.target.checked })} />
              Send me a copy ({s.ownerEmail})
            </label>
          </fieldset>
          {note ? (
            <p role="status" className="meta">
              {note}
            </p>
          ) : null}
        </div>
      )}
    </section>
  );
}
