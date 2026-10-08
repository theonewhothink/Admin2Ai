"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import detail from "@/components/detail/detail.module.css";
import { Icon, type IconName } from "@/components/Icon";
import { liveData } from "@/lib/api";

const LINKS: { href: string; label: string; detail: string; icon: IconName }[] = [
  { href: "/settings/people", label: "People and expenses", detail: "Company cards and receipts people paid themselves", icon: "users" },
  { href: "/deadlines", label: "Deadlines", detail: "Every deadline from your letters, and what proves it done", icon: "clock" },
  { href: "/settings/plan", label: "Plan", detail: "Your plan, what you use, payment details", icon: "bookmark" },
];

/** People, deadlines and the plan, one tap from Settings. */
export function SettingsLinks() {
  if (!liveData) return null;
  return (
    <section aria-labelledby="more-h">
      <div className="section-head">
        <h2 id="more-h" className="h2">
          Your business
        </h2>
      </div>
      <ul className="card list">
        {LINKS.map((l) => (
          <li key={l.href}>
            <Link href={l.href} className={detail.linkRow}>
              <Icon name={l.icon} size={20} style={{ color: "var(--text-2)" }} />
              <span className={detail.rowMain}>
                <span className={detail.rowTitle}>{l.label}</span>
                <span className="meta">{l.detail}</span>
              </span>
              <Icon name="chevronRight" size={18} className={detail.chevron} />
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

const RETURNS: Record<string, string> = {
  done: "Thank you. Your plan changes as soon as the payment is confirmed. It takes a minute.",
  changed: "Thank you. Your plan changes as soon as Stripe confirms it.",
  cancelled: "Nothing was charged. Your plan is as it was.",
  back: "Welcome back. Any change you made on the payment page shows here in a minute.",
};

/** Back from Stripe's payment page (`/settings/?billing=done|cancelled|changed|back`): one calm line. */
export function BillingReturn() {
  const billing = useSearchParams().get("billing");
  const text = billing ? RETURNS[billing] : undefined;
  if (!text) return null;
  return (
    <div className="notice" role="status">
      <Icon name="bookmark" size={18} />
      <p style={{ flex: 1 }}>
        {text}{" "}
        <Link href="/settings/plan" className="link">
          Your plan
        </Link>
      </p>
    </div>
  );
}
