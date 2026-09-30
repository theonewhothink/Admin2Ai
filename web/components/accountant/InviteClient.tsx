"use client";

import { useEffect, useState } from "react";
import { Icon } from "@/components/Icon";
import { getInvitations, inviteClient, liveData } from "@/lib/api";
import type { AccountantInvitation } from "@/lib/types";
import styles from "./accountant.module.css";

const TONE: Record<string, string> = {
  accepted: "pill-good",
  expired: "pill-attention",
  waiting: "pill-attention",
  not_sent: "pill-risk",
};

/**
 * Invite a client business (§29): they receive "Your accountant has enabled Back Office for you.",
 * connect their email and bank, and you see their month here once they accept.
 */
export function InviteClient() {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [nif, setNif] = useState("");
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const [invites, setInvites] = useState<AccountantInvitation[]>([]);

  useEffect(() => {
    let live = true;
    void getInvitations().then((list) => {
      if (live) setInvites(list);
    });
    return () => {
      live = false;
    };
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email.trim() || saving) return;
    setSaving(true);
    setResult(null);
    const out = await inviteClient({
      email: email.trim(),
      ...(name.trim() ? { clientName: name.trim() } : {}),
      ...(nif.trim() ? { taxIds: [nif.trim()] } : {}),
    });
    setSaving(false);
    setResult({ ok: out.ok, text: out.message ?? (out.ok ? "Done." : "I couldn’t send that. Try again.") });
    if (out.ok) {
      setEmail("");
      setName("");
      setNif("");
      setInvites(await getInvitations());
    }
  };

  return (
    <section className={`card ${styles.teach}`} aria-labelledby="invite-h">
      <div className="stack-1">
        <h2 id="invite-h" className="h2">
          Invite a client
        </h2>
        <p className="muted">
          They receive “Your accountant has enabled Back Office for you.” Once they connect their email and bank and
          accept, their month appears here.
        </p>
      </div>
      <form className={styles.inviteForm} onSubmit={submit}>
        <label className={styles.field}>
          <span>Client’s email</span>
          <input
            className="input"
            type="email"
            required
            autoComplete="off"
            placeholder="owner@client.pt"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </label>
        <label className={styles.field}>
          <span>Business name (optional)</span>
          <input className="input" autoComplete="off" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className={styles.field}>
          <span>Only this company’s NIF (optional)</span>
          <input
            className="input"
            inputMode="numeric"
            autoComplete="off"
            placeholder="9 digits"
            value={nif}
            onChange={(e) => setNif(e.target.value)}
          />
        </label>
        <div>
          <button type="submit" className="btn btn-primary" disabled={!email.trim() || saving || !liveData}>
            <Icon name="send" size={18} />
            {saving ? "Sending…" : "Send invitation"}
          </button>
        </div>
      </form>
      <div aria-live="polite">
        {!liveData ? <p className={styles.empty}>Connect the backend to send invitations.</p> : null}
        {result ? (
          <p className={result.ok ? styles.teachResult : `risk-text ${styles.teachResult}`} role={result.ok ? "status" : "alert"}>
            {result.ok ? <Icon name="check" size={18} strokeWidth={2.2} /> : null}
            {result.text}
          </p>
        ) : null}
      </div>
      {invites.length > 0 ? (
        <div className="stack-1">
          <p className="meta">Invitations</p>
          <ul className={styles.rules}>
            {invites.map((i) => (
              <li key={i.id}>
                <Icon name="mail" size={16} />
                <span className={styles.inviteLine}>
                  {i.clientName ? `${i.clientName} · ` : ""}
                  {i.email}
                  {i.taxIds.length ? <span className="meta"> · NIF {i.taxIds.join(", ")}</span> : null}
                </span>
                <span className={`pill ${TONE[i.status] ?? ""}`}>{i.statusLabel}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
