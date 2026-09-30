"use client";

import { useCallback, useEffect, useState } from "react";
import { Icon } from "@/components/Icon";
import { call, download, query } from "@/lib/api";
import { formatMoney } from "@/lib/format";
import { ReportDelivery } from "./ReportDelivery";

interface Doc {
  id: string;
  supplier: string;
  number: string;
  type: string;
  date: string;
  amount: number | null;
  currency: string;
  company: string;
  status: string;
  filename: string;
}
interface DocList {
  items: Doc[];
  companies: { id: string; name: string }[];
  total: number;
  message?: string;
}
interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  createdAt: string;
  scope: string;
}

function AccountantApi() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [fresh, setFresh] = useState<string | null>(null);
  const [name, setName] = useState("TOConline");
  const load = useCallback(async () => {
    const r = await call<{ keys: ApiKey[] }>("GET", "/api/accountant/api-keys");
    if (r.ok) setKeys(r.body.keys);
  }, []);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loads data, sets state after the request
    void load();
  }, [load]);
  const base = process.env.NEXT_PUBLIC_API_URL || "https://api.your-backoffice.eu";
  return (
    <section aria-labelledby="api-h" className="stack-2">
      <div className="section-head">
        <h2 id="api-h" className="h2">
          Accountant access
        </h2>
      </div>
      <p className="meta">
        Give your accountant’s software a read-only key. It can list every document, download originals and pull a whole
        period as one ZIP.
      </p>
      <form
        className="card card-pad"
        style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "end" }}
        onSubmit={async (e) => {
          e.preventDefault();
          const r = await call<{ key: string }>("POST", "/api/accountant/api-keys", { name });
          if (r.ok) setFresh(r.body.key);
          await load();
        }}
      >
        <label style={{ display: "grid", gap: 6, flex: 1, minWidth: 180 }}>
          <span className="label">Used by</span>
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <button className="btn btn-primary" type="submit">
          Create key
        </button>
      </form>
      {fresh ? (
        <div className="card card-pad" role="status" style={{ display: "grid", gap: 6 }}>
          <strong>Copy this key now. I only show it once.</strong>
          <code style={{ overflowWrap: "anywhere" }}>{fresh}</code>
        </div>
      ) : null}
      {keys.length ? (
        <ul className="card list">
          {keys.map((k) => (
            <li key={k.id} className="list-row">
              <Icon name="lock" size={18} style={{ color: "var(--text-2)" }} />
              <span style={{ flex: 1, display: "grid" }}>
                <span style={{ fontWeight: 600 }}>{k.name}</span>
                <span className="meta">
                  {k.prefix}… · read documents · created {k.createdAt.slice(0, 10)}
                </span>
              </span>
              <button
                className="btn btn-quiet"
                type="button"
                onClick={async () => {
                  await call("POST", `/api/accountant/api-keys/${k.id}/revoke`, {});
                  await load();
                }}
              >
                Revoke
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      <details className="card card-pad">
        <summary style={{ cursor: "pointer", fontWeight: 600 }}>How to connect accounting software</summary>
        <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", fontSize: 13, marginTop: 12 }}>
{`# List documents (filters: company, from, to, supplier, q)
curl -H "Authorization: Bearer <key>" "${base}/api/v1/documents?from=2026-09-01&to=2026-09-30"

# Download one original
curl -H "Authorization: Bearer <key>" -OJ "${base}/api/v1/documents/<id>/file"

# Whole period as ZIP (originals + ledger.csv + manifest.json with SHA-256 per file)
curl -H "Authorization: Bearer <key>" -OJ "${base}/api/v1/export?from=2026-09-01&to=2026-09-30"`}
        </pre>
      </details>
    </section>
  );
}

export function DocumentsView() {
  const [company, setCompany] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [q, setQ] = useState("");
  const [data, setData] = useState<DocList | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    const r = await call<DocList>("GET", `/api/documents${query({ company, from, to, q })}`);
    setData(r.ok ? r.body : { items: [], companies: data?.companies ?? [], total: 0, message: r.body.message });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [company, from, to, q]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- loads data, sets state after the request
    void load();
  }, [load]);

  return (
    <div className="container-narrow page">
      <header className="page-head">
        <h1 className="h1">Documents</h1>
        <p className="lead">Every invoice, receipt and letter I have collected, with the original file. Download any period in one go.</p>
      </header>

      <div className="stack-6">
        <section aria-label="Filter" className="card card-pad" style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(150px, 1fr))" }}>
          <label style={{ display: "grid", gap: 6 }}>
            <span className="label">Company</span>
            <select className="input" value={company} onChange={(e) => setCompany(e.target.value)}>
              <option value="">All companies</option>
              {(data?.companies ?? []).map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <label style={{ display: "grid", gap: 6 }}>
            <span className="label">From</span>
            <input className="input" type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label style={{ display: "grid", gap: 6 }}>
            <span className="label">To</span>
            <input className="input" type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          </label>
          <label style={{ display: "grid", gap: 6 }}>
            <span className="label">Search</span>
            <input className="input" value={q} placeholder="Supplier or number" onChange={(e) => setQ(e.target.value)} />
          </label>
        </section>

        <section aria-labelledby="docs-h" className="stack-2">
          <div className="section-head">
            <h2 id="docs-h" className="h2">
              {data ? `${data.total} document${data.total === 1 ? "" : "s"}` : "Documents"}
            </h2>
            <button
              className="btn btn-secondary"
              type="button"
              disabled={!data?.total}
              onClick={async () => setNote(await download("/api/documents/export", "POST", { company, from, to }))}
            >
              Download all (ZIP)
            </button>
          </div>
          {note ? <p className="meta">{note}</p> : null}
          {data?.message ? <p className="card card-pad meta">{data.message}</p> : null}
          {data && data.items.length ? (
            <ul className="card list">
              {data.items.map((d) => (
                <li key={d.id} className="list-row">
                  <Icon name="document" size={18} style={{ color: "var(--text-2)", flexShrink: 0 }} />
                  <span style={{ flex: 1, minWidth: 0, display: "grid" }}>
                    <span style={{ fontWeight: 600, overflowWrap: "anywhere" }}>
                      {d.supplier} {d.number}
                    </span>
                    <span className="meta" style={{ overflowWrap: "anywhere" }}>
                      {d.date} · {d.company || "No company yet"} · {d.type} · {d.status}
                    </span>
                  </span>
                  <span className="tabular" style={{ fontWeight: 600 }}>
                    {d.amount === null ? "—" : formatMoney(d.amount, d.currency)}
                  </span>
                  <button className="btn btn-quiet" type="button" aria-label={`Download ${d.supplier} ${d.number}`} onClick={async () => setNote(await download(`/api/documents/${d.id}/file`))}>
                    Download
                  </button>
                </li>
              ))}
            </ul>
          ) : data && !data.message ? (
            <p className="card card-pad meta">Nothing in this period.</p>
          ) : null}
        </section>

        <ReportDelivery />
        <AccountantApi />
      </div>
    </div>
  );
}
