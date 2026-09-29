"use client";

import Link from "next/link";
import { useCallback, useEffect, useId, useRef, useState, useSyncExternalStore } from "react";
import { Icon } from "@/components/Icon";
import { call, download } from "@/lib/api";
import { askClaude, claudeKey, looksLikeKey, setClaudeKey, subscribeClaudeKey } from "@/lib/claude";
import { evidenceHref } from "@/lib/evidence";
import { formatDayShort, formatMoney } from "@/lib/format";

/* Cards returned by POST /api/chat (backend/src/backoffice/assistant.py). */
interface DocItem {
  id: string;
  supplier: string;
  number: string;
  date: string;
  amount: number | null;
  company: string;
  status: string;
}
interface TaskItem {
  id: string;
  title: string;
  due: string | null;
  company: string | null;
  status: string;
}
type Card =
  | { type: "documents"; items: DocItem[] }
  | { type: "tasks"; items: TaskItem[] }
  | { type: "evidence"; items: { id: string; label: string }[] }
  | {
      type: "report";
      id: string;
      title: string;
      spent: number;
      received: number;
      payments: number;
      documents: number;
      missingInvoices: number;
      topSuppliers: { name: string; amount: number }[];
    }
  | {
      type: "summary";
      supplier: string;
      from: string;
      to: string;
      payments: number;
      spent: number;
      average: number;
      documents: number;
      byMonth: { month: string; amount: number }[];
      issues: string[];
      coverageNote: string;
    }
  | {
      type: "email";
      id: string;
      to: string[];
      subject: string;
      body: string;
      attachments: { kind: string; id: string; name: string }[];
      status: string;
    };

interface Turn {
  id: number;
  role: "user" | "assistant";
  text: string;
  cards?: Card[];
  pending?: boolean;
}

const money = (n: number) => formatMoney(n, "EUR");

function Documents({ items }: { items: DocItem[] }) {
  const [note, setNote] = useState<string | null>(null);
  return (
    <ul className="card list">
      {items.map((d) => (
        <li key={d.id} className="list-row">
          <Icon name="document" size={18} style={{ color: "var(--text-2)", flexShrink: 0 }} />
          <span style={{ flex: 1, minWidth: 0, display: "grid" }}>
            <span style={{ fontWeight: 600, overflowWrap: "anywhere" }}>
              {d.supplier} {d.number}
            </span>
            <span className="meta">
              {d.date} · {d.company || "No company yet"} · {d.status}
            </span>
          </span>
          <span className="tabular" style={{ fontWeight: 600 }}>
            {d.amount === null ? "—" : money(d.amount)}
          </span>
          <button className="btn btn-quiet" type="button" onClick={async () => setNote(await download(`/api/documents/${d.id}/file`))}>
            Download
          </button>
        </li>
      ))}
      {note ? <li className="list-row meta">{note}</li> : null}
    </ul>
  );
}

function EmailDraft({ card }: { card: Extract<Card, { type: "email" }> }) {
  const [status, setStatus] = useState(card.status);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function act(cancel: boolean) {
    setBusy(true);
    const r = await call<{ status: string; message: string }>("POST", `/api/chat/outbox/${card.id}/send`, cancel ? { cancel: true } : {});
    setBusy(false);
    setStatus(r.body.status ?? status);
    setNote(r.body.message ?? null);
  }
  return (
    <div className="card card-pad" style={{ display: "grid", gap: 8 }}>
      <span className="meta">Email · {status === "draft" ? "ready to send" : status}</span>
      <span>
        <strong>To:</strong> {card.to.join(", ")}
      </span>
      <span>
        <strong>Subject:</strong> {card.subject}
      </span>
      <p style={{ whiteSpace: "pre-wrap", color: "var(--text-2)" }}>{card.body}</p>
      {card.attachments.length ? (
        <span className="meta">
          <Icon name="link" size={14} /> {card.attachments.map((a) => a.name).join(", ")}
        </span>
      ) : null}
      {status === "draft" ? (
        <div style={{ display: "flex", gap: 8 }}>
          <button className="btn btn-primary" type="button" disabled={busy} onClick={() => void act(false)}>
            Send email
          </button>
          <button className="btn btn-quiet" type="button" disabled={busy} onClick={() => void act(true)}>
            Don’t send
          </button>
        </div>
      ) : null}
      {note ? (
        <p role="status" className="meta">
          {note}
        </p>
      ) : null}
    </div>
  );
}

function Tasks({ items }: { items: TaskItem[] }) {
  const [list, setList] = useState(items);
  const [note, setNote] = useState<string | null>(null);
  async function done(id: string) {
    const r = await call<{ task?: TaskItem; message?: string }>("POST", `/api/tasks/${encodeURIComponent(id)}/done`, {});
    const task = r.body.task;
    if (r.ok && task) setList((prev) => prev.map((t) => (t.id === id ? task : t)));
    else setNote(r.body.message ?? "I couldn't update that task.");
  }
  return (
    <ul className="card list" aria-label="Tasks">
      {list.map((t) => {
        const open = t.status === "open";
        return (
          <li key={t.id} className="list-row">
            {open ? (
              <button className="btn btn-quiet" type="button" onClick={() => void done(t.id)} aria-label={`Mark “${t.title}” as done`}>
                <Icon name="check" size={16} />
              </button>
            ) : (
              <Icon name="checkCircle" size={20} style={{ color: "var(--good)", flexShrink: 0, margin: "0 10px" }} />
            )}
            <span style={{ flex: 1, minWidth: 0, display: "grid" }}>
              <span style={{ fontWeight: 600, overflowWrap: "anywhere", textDecoration: open ? undefined : "line-through", color: open ? undefined : "var(--text-2)" }}>
                {t.title}
              </span>
              {t.due || t.company ? (
                <span className="meta">{[t.due ? `Due ${formatDayShort(t.due)}` : null, t.company].filter(Boolean).join(" · ")}</span>
              ) : null}
            </span>
          </li>
        );
      })}
      {note ? <li className="list-row meta">{note}</li> : null}
    </ul>
  );
}

function CardView({ card }: { card: Card }) {
  const [note, setNote] = useState<string | null>(null);
  if (card.type === "documents") return <Documents items={card.items} />;
  if (card.type === "tasks") return <Tasks items={card.items} />;
  if (card.type === "email") return <EmailDraft card={card} />;
  if (card.type === "evidence") {
    return (
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        {card.items.map((e) => {
          const href = evidenceHref(e);
          return href ? (
            <Link key={e.id} href={href} className="chip">
              {e.label}
            </Link>
          ) : (
            <span key={e.id} className="chip">
              {e.label}
            </span>
          );
        })}
      </div>
    );
  }
  if (card.type === "report") {
    return (
      <div className="card card-pad" style={{ display: "grid", gap: 8 }}>
        <strong>{card.title}</strong>
        <span className="tabular">
          Spent {money(card.spent)} · Received {money(card.received)} · {card.payments} payments · {card.documents} documents
        </span>
        <span className="meta">
          {card.missingInvoices === 0 ? "Every payment has its invoice." : `${card.missingInvoices} payment(s) still without an invoice.`}
        </span>
        {card.topSuppliers.length ? (
          <ul className="list" style={{ margin: 0 }}>
            {card.topSuppliers.map((s) => (
              <li key={s.name} className="list-row" style={{ padding: "4px 0" }}>
                <span style={{ flex: 1 }}>{s.name}</span>
                <span className="tabular">{money(s.amount)}</span>
              </li>
            ))}
          </ul>
        ) : null}
        <div>
          <button className="btn btn-secondary" type="button" onClick={async () => setNote(await download(`/api/reports/${card.id}/file`))}>
            Download CSV
          </button>
        </div>
        {note ? <span className="meta">{note}</span> : null}
      </div>
    );
  }
  const max = Math.max(1, ...card.byMonth.map((m) => m.amount));
  return (
    <div className="card card-pad" style={{ display: "grid", gap: 8 }}>
      <strong>{card.supplier}</strong>
      <span className="tabular">
        {money(card.spent)} · {card.payments} payments · average {money(card.average)} · {card.documents} document{card.documents === 1 ? "" : "s"}
      </span>
      {card.coverageNote ? <span className="meta">{card.coverageNote}</span> : null}
      <ul style={{ listStyle: "none", padding: 0, margin: 0, display: "grid", gap: 4 }}>
        {card.byMonth.map((m) => (
          <li key={m.month} style={{ display: "grid", gridTemplateColumns: "72px 1fr 88px", gap: 8, alignItems: "center" }}>
            <span className="meta tabular">{m.month}</span>
            <span style={{ height: 8, borderRadius: 4, background: "var(--good)", width: `${(m.amount / max) * 100}%` }} />
            <span className="tabular" style={{ textAlign: "right" }}>
              {money(m.amount)}
            </span>
          </li>
        ))}
      </ul>
      {card.issues.length ? (
        <div style={{ display: "grid", gap: 4 }}>
          <span className="meta">To look at</span>
          {card.issues.map((i) => (
            <span key={i} style={{ color: "var(--attention)" }}>
              • {i}
            </span>
          ))}
        </div>
      ) : (
        <span className="meta">No issues found.</span>
      )}
    </div>
  );
}

const noKey = () => null;

/** Who answers: Claude with the owner's own key, or the built-in rules. Lets the owner add or remove the key. */
function BrainSettings({ open, onToggle, compact }: { open: boolean; onToggle: () => void; compact: boolean }) {
  const key = useSyncExternalStore(subscribeClaudeKey, claudeKey, noKey);
  const [draft, setDraft] = useState("");
  const [note, setNote] = useState<string | null>(null);
  const inputId = useId();
  function save() {
    if (!looksLikeKey(draft)) {
      setNote("That doesn’t look like an Anthropic key. It starts with sk-ant-.");
      return;
    }
    setNote(setClaudeKey(draft) ? null : "This browser won’t keep the key. Check that site data is allowed.");
    setDraft("");
    if (open) onToggle();
  }
  return (
    <div style={{ display: "grid", gap: 8 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span className="meta" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
          <span aria-hidden="true" style={{ width: 8, height: 8, borderRadius: 4, background: key ? "var(--good-dot)" : "var(--text-3)" }} />
          {key ? "Answers by Claude" : compact ? "Basic answers" : "Basic answers. Connect Claude to ask anything."}
        </span>
        <button className="btn btn-quiet" type="button" onClick={onToggle} aria-expanded={open}>
          {key ? "Settings" : "Connect Claude"}
        </button>
      </div>
      {open ? (
        <div className="card card-pad" style={{ display: "grid", gap: 8 }}>
          {key ? (
            <>
              <p>Claude answers with your Anthropic key, saved in this browser.</p>
              <div>
                <button
                  className="btn btn-secondary"
                  type="button"
                  onClick={() => {
                    setClaudeKey(null);
                    onToggle();
                  }}
                >
                  Remove key
                </button>
              </div>
            </>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                save();
              }}
              style={{ display: "grid", gap: 8 }}
            >
              <label htmlFor={inputId}>Your Anthropic API key</label>
              <input
                id={inputId}
                className="input"
                type="password"
                autoComplete="off"
                spellCheck={false}
                placeholder="sk-ant-…"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
              />
              <p className="meta">
                Kept only in this browser and sent only to Anthropic. Create one at{" "}
                <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">
                  console.anthropic.com
                </a>
                . Usage is billed to your Anthropic account.
              </p>
              <div>
                <button className="btn btn-primary" type="submit" disabled={!draft.trim()}>
                  Save key
                </button>
              </div>
            </form>
          )}
          {note ? (
            <p role="status" className="meta">
              {note}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

export function ChatClient({
  initialQuestion,
  examples,
  compact = false,
}: {
  initialQuestion?: string;
  examples: string[];
  /** Inside the chat panel: fewer examples and the message box pinned to the panel's bottom. */
  compact?: boolean;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [value, setValue] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false);
  const nextId = useRef(1);
  const started = useRef(false);
  const end = useRef<HTMLDivElement>(null);
  const busy = turns.some((t) => t.pending);
  const inputId = useId();

  const send = useCallback(
    (text: string) => {
      const message = text.trim();
      if (!message) return;
      const uid = nextId.current++;
      const aid = nextId.current++;
      const history = turns.filter((t) => !t.pending).map((t) => ({ role: t.role, content: t.text }));
      setTurns((prev) => [...prev, { id: uid, role: "user", text: message }, { id: aid, role: "assistant", text: "", pending: true }]);
      setValue("");
      const finish = (reply: string, cards: Card[]) =>
        setTurns((prev) => prev.map((t) => (t.id === aid ? { ...t, text: reply, cards, pending: false } : t)));
      const key = claudeKey();
      if (key) {
        void askClaude<Card>(key, message, history).then((r) => {
          finish(r.reply, r.cards);
          if (r.keyProblem) setSettingsOpen(true);
        });
        return;
      }
      void call<{ reply?: string; cards?: Card[]; message?: string }>("POST", "/api/chat", { message, history }).then((r) => {
        finish(r.ok ? (r.body.reply ?? "Done.") : (r.body.message ?? "I couldn't do that. Try again."), r.body.cards ?? []);
      });
    },
    [turns],
  );

  useEffect(() => {
    if (initialQuestion && !started.current) {
      started.current = true;
      send(initialQuestion);
    }
  }, [initialQuestion, send]);

  useEffect(() => {
    end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns]);

  return (
    <div style={{ display: "grid", gap: 16 }}>
      <BrainSettings open={settingsOpen} onToggle={() => setSettingsOpen((o) => !o)} compact={compact} />

      {turns.length === 0 ? (
        <section className="stack-2" aria-label="Examples">
          <p className="meta">Ask a question or give me a task</p>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
            {(compact ? examples.slice(0, 5) : examples).map((q) => (
              <button key={q} type="button" className="chip" onClick={() => send(q)}>
                {q}
              </button>
            ))}
          </div>
        </section>
      ) : null}

      <div aria-live="polite" style={{ display: "grid", gap: 16 }}>
        {turns.map((t) =>
          t.role === "user" ? (
            <p
              key={t.id}
              style={{
                justifySelf: "end",
                maxWidth: "85%",
                background: "var(--text)",
                color: "var(--on-ink)",
                padding: "10px 14px",
                borderRadius: 16,
                overflowWrap: "anywhere",
                whiteSpace: "pre-wrap",
              }}
            >
              {t.text}
            </p>
          ) : (
            <div key={t.id} style={{ display: "grid", gap: 12 }}>
              <p style={{ overflowWrap: "anywhere", whiteSpace: "pre-wrap" }}>{t.pending ? "Working on it…" : t.text}</p>
              {(t.cards ?? []).map((c, i) => (
                <CardView key={`${t.id}-${i}`} card={c} />
              ))}
            </div>
          ),
        )}
        <div ref={end} />
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          send(value);
        }}
        className="card"
        style={{ display: "flex", gap: 8, padding: 8, position: "sticky", bottom: compact ? 0 : 96 }}
      >
        <label htmlFor={inputId} className="sr-only">
          Message
        </label>
        <input
          id={inputId}
          className="input"
          style={{ flex: 1, border: "none" }}
          placeholder="Ask anything or give me a task…"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoFocus={!initialQuestion}
          autoComplete="off"
          maxLength={4000}
        />
        <button className="btn btn-primary" type="submit" disabled={busy || !value.trim()} aria-label="Send message">
          <Icon name="arrowRight" size={18} />
        </button>
      </form>
    </div>
  );
}
