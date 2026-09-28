/**
 * Response shapes of the owner API, shared with the web app's contract
 * (web/README.md "Connecting the backend"). Money arrives as a JSON string or
 * number and is normalised to a decimal string by ./guards.ts.
 */
import type { DecimalString } from "../lib/money";

/** Emerald = all good / closed, amber = attention, red = real risk (§30-33). */
export type Tone = "good" | "attention" | "risk" | "neutral";

export interface CompanySummary {
  id: string;
  name: string;
  tone: Tone;
  /** "On track", "Closed", "Needs one answer". */
  statusLabel: string;
  /** "September · 94% closed". */
  detail: string;
  pendingItemIds?: string[];
}

export interface Connection {
  id: string;
  name: string;
  kind: "email" | "bank" | "accountant";
  account: string;
  status: "healthy" | "stale";
  lastSyncedAt: string;
  lastSyncedLabel?: string;
}

export interface HandledStat {
  id: string;
  count: number;
  label: string;
}

export interface HomeData {
  greeting?: string;
  needsYouCount: number;
  currentMonth: { key: string; label: string; percentClosed: number };
  companies: CompanySummary[];
  handled: HandledStat[];
  handledPeriodLabel?: string;
  /** Mobile Home tile (§41). Falls back to `handled` when the period is "Today". */
  handledToday?: number;
  connections: Connection[];
}

export interface DecisionOption {
  id: string;
  label: string;
  /** One-tap follow-up choices, e.g. "Another company" → Company B / Company C. */
  choices?: Array<{ id: string; label: string }>;
}

export interface RememberRule {
  /** "Always use {choice} for IKEA paid with card •••• 4817". */
  template: string;
  overrides?: Record<string, string>;
  defaultChecked: boolean;
}

interface NeedsYouBase {
  id: string;
  tone: Tone;
  eyebrow: string;
  merchant: string;
  amount: DecimalString | null;
  currency: string;
  date: string;
  companyId?: string;
  why: string[];
}

export interface NeedsYouChoiceItem extends NeedsYouBase {
  kind: "choice";
  paidWith?: string;
  question: string;
  options: DecisionOption[];
  remember?: RememberRule;
}

/** Hard approval (§25, §26): never one tap, never remembered. */
export interface NeedsYouApprovalItem extends NeedsYouBase {
  kind: "approval";
  title: string;
  body: string;
  facts: Array<{ label: string; value: string; tone?: Tone }>;
  verification: {
    optionLabel: string;
    instruction: string;
    checkboxLabel: string;
    confirmLabel: string;
    confirmOptionId: string;
    confirmedMessage: string;
  };
  keepBlocked: { label: string; optionId: string; message: string };
}

export type NeedsYouItem = NeedsYouChoiceItem | NeedsYouApprovalItem;

export type ActivityKind =
  | "collected"
  | "recovered"
  | "chased"
  | "answered"
  | "checked"
  | "closed"
  | "protected"
  | "learned";

export interface ActivityItem {
  id: string;
  at: string;
  kind: ActivityKind;
  text: string;
  companyName?: string;
  amount?: DecimalString;
  currency?: string;
}

export interface ActivityFeed {
  today?: string;
  items: ActivityItem[];
}

export interface EvidenceLink {
  label: string;
  id: string;
}

export interface AskAnswer {
  answer: string;
  /** Answers point at evidence; AI memory is never financial evidence (§34-41). */
  evidence: EvidenceLink[];
}
