"use client";

/** Building blocks of the internal dashboard: header, section labels, cards, rings, gauge, bars. */
import type { CSSProperties, ReactNode } from "react";
import { AdminIcon, type AdminIconName } from "./icons";
import styles from "./admin.module.css";

export const COLORS = {
  amber: "#f59e0b",
  blue: "#3b82f6",
  purple: "#a855f7",
  green: "#22c55e",
  red: "#ef4444",
  slate: "#64748b",
} as const;

export type ColorName = keyof typeof COLORS;

/** ≥80 green, ≥60 amber, below that red. */
export function scoreColor(score: number | null): string {
  if (score === null) return COLORS.slate;
  return score >= 80 ? COLORS.green : score >= 60 ? COLORS.amber : COLORS.red;
}

export function PageHeader({
  title,
  subtitle,
  loading,
  onRefresh,
}: {
  title: string;
  subtitle: string;
  loading?: boolean;
  onRefresh?: () => void;
}) {
  return (
    <header className={styles.pageHead}>
      <div>
        <h1 className={styles.title}>{title}</h1>
        <p className={styles.subtitle}>{subtitle}</p>
      </div>
      {onRefresh ? (
        <button type="button" className={styles.ghostButton} onClick={onRefresh} disabled={loading} aria-busy={loading}>
          <AdminIcon name="refresh" size={16} className={loading ? styles.spin : undefined} />
          {loading ? "Refreshing" : "Refresh"}
        </button>
      ) : null}
    </header>
  );
}

export function SectionLabel({ children, id }: { children: ReactNode; id?: string }) {
  return (
    <h2 className={styles.sectionLabel} id={id}>
      {children}
    </h2>
  );
}

export function Card({
  children,
  className,
  delay = 0,
  style,
  as: Tag = "section",
  ...rest
}: {
  children: ReactNode;
  className?: string;
  delay?: number;
  style?: CSSProperties;
  as?: "section" | "div" | "article" | "li";
  "aria-labelledby"?: string;
  "aria-label"?: string;
}) {
  return (
    <Tag
      className={`${styles.card} ${className ?? ""}`}
      style={{ ...style, animationDelay: `${delay}ms` }}
      {...rest}
    >
      {children}
    </Tag>
  );
}

export function Chip({ icon, color, size = 18 }: { icon: AdminIconName; color: string; size?: number }) {
  return (
    <span className={styles.chip} style={{ "--chip": color } as CSSProperties}>
      <AdminIcon name={icon} size={size} />
    </span>
  );
}

export function Bar({ percent, color, label, height = 6 }: { percent: number; color: string; label?: string; height?: number }) {
  const p = Math.max(0, Math.min(100, percent));
  return (
    <div
      className={styles.bar}
      style={{ height }}
      role={label ? "img" : undefined}
      aria-label={label}
      aria-hidden={label ? undefined : true}
    >
      <span style={{ width: `${p}%`, background: color }} />
    </div>
  );
}

const R = 36;
const C = 2 * Math.PI * R;

/** Progress ring: r=36, 8px stroke, round cap, the percent in the middle. */
export function Ring({
  percent,
  color,
  label,
  caption,
  ariaLabel,
}: {
  percent: number;
  color: string;
  label: string;
  caption?: string;
  ariaLabel: string;
}) {
  const p = Math.max(0, Math.min(100, percent));
  return (
    <div className={styles.ring} role="img" aria-label={ariaLabel}>
      <svg viewBox="0 0 88 88" width="88" height="88" aria-hidden="true">
        <circle cx="44" cy="44" r={R} fill="none" stroke="#1e293b" strokeWidth="8" />
        {p > 0 ? (
          <circle
            cx="44"
            cy="44"
            r={R}
            fill="none"
            stroke={color}
            strokeWidth="8"
            strokeLinecap="round"
            strokeDasharray={C}
            strokeDashoffset={C * (1 - p / 100)}
            transform="rotate(-90 44 44)"
            className={styles.ringArc}
          />
        ) : null}
      </svg>
      <span className={styles.ringText}>
        <strong>{label}</strong>
        {caption ? <small>{caption}</small> : null}
      </span>
    </div>
  );
}

/** Semicircle gauge (M 10 80 A 70 70 0 0 1 150 80), coloured by score, with a needle. */
export function Gauge({ score }: { score: number | null }) {
  const s = score === null ? 0 : Math.max(0, Math.min(100, score));
  const color = scoreColor(score);
  const angle = Math.PI - (s / 100) * Math.PI;
  const needle = 30; // short, so it never crosses the score
  const x = 80 + needle * Math.cos(angle);
  const y = 80 - needle * Math.sin(angle);
  return (
    <div className={styles.gauge} role="img" aria-label={score === null ? "No health score yet" : `Health score ${score} out of 100`}>
      <svg viewBox="0 0 160 92" width="200" height="115" aria-hidden="true">
        <path d="M 10 80 A 70 70 0 0 1 150 80" fill="none" stroke="#1e293b" strokeWidth="12" strokeLinecap="round" />
        {s > 0 ? (
          <path
            d="M 10 80 A 70 70 0 0 1 150 80"
            fill="none"
            stroke={color}
            strokeWidth="12"
            strokeLinecap="round"
            pathLength={100}
            strokeDasharray={`${s} 100`}
          />
        ) : null}
        <line x1="80" y1="80" x2={x.toFixed(2)} y2={y.toFixed(2)} stroke="#ffffff" strokeWidth="3" strokeLinecap="round" />
        <circle cx="80" cy="80" r="5" fill="#ffffff" />
      </svg>
      <span className={styles.gaugeScore} style={{ color }}>
        {score === null ? "–" : score}
      </span>
    </div>
  );
}

export function Pill({ tone, children }: { tone: "green" | "amber" | "red" | "blue" | "slate" | "purple"; children: ReactNode }) {
  return (
    <span className={styles.pill} data-tone={tone}>
      {children}
    </span>
  );
}

/** Placeholder while the engine starts, or a message when there is nothing to ask. */
export function Waiting({ loaded, what }: { loaded: boolean; what: string }) {
  return (
    <div className={`${styles.card} ${styles.waiting}`} role="status" aria-live="polite">
      {loaded ? (
        <>
          <p className={styles.waitingTitle}>I couldn’t load {what}.</p>
          <p className={styles.muted}>
            The dashboard reads the engine’s own records. Open the live site, or connect the backend, then press Refresh.
          </p>
        </>
      ) : (
        <>
          <span className={styles.pulse} aria-hidden="true" />
          <p className={styles.waitingTitle}>Loading {what}…</p>
          <p className={styles.muted}>The first visit starts the engine in your browser. It takes a few seconds.</p>
        </>
      )}
    </div>
  );
}

const TIME = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "Europe/Lisbon",
});

/** 2 Oct, 09:12 (Lisbon time, like the engine). */
export function when(iso: string | null): string {
  return iso ? TIME.format(new Date(iso)) : "–";
}

/** "18 min ago" / "3 h ago" / "2 days ago", measured from the engine's own clock. */
export function ago(iso: string | null, now: string): string {
  if (!iso) return "never";
  const minutes = Math.round((new Date(now).getTime() - new Date(iso).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}
