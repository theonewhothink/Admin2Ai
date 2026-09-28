/**
 * Shared types for the web app. These double as the expected response shapes
 * of the backend endpoints (see README.md, "API contract").
 */

/** Visual tone. Emerald = all good / closed, amber = attention, red = real risk. */
export type Tone = "good" | "attention" | "risk" | "neutral";

export type ISODate = string; // "2026-10-02"
export type ISODateTime = string; // "2026-10-02T09:12:00+02:00"
export type MonthKey = string; // "2026-09"

export interface Owner {
  firstName: string;
  fullName: string;
  email: string;
  initials: string;
}

export interface CompanySummary {
  id: string;
  name: string;
  legalName: string;
  taxId: string;
  tone: Tone;
  /** "On track", "Closed", "Needs one answer" */
  statusLabel: string;
  /** Short plain-language detail, e.g. "September · 94% closed" */
  detail: string;
  /** Month shown by default on the company page. */
  currentMonth: MonthKey;
  /** Months that can be opened on the company page, newest first. */
  months: MonthKey[];
  /** Needs-you items that hold this company's status. When all are answered, the status reads "On track". */
  pendingItemIds?: string[];
}

export interface DueItem {
  id: string;
  title: string;
  companyName: string;
  due: ISODate;
  note: string;
  tone: Tone;
  href?: string;
}

export interface HandledStat {
  id: string;
  count: number;
  /** Label read after the number: "documents collected" */
  label: string;
}

export type ConnectionKind = "email" | "bank" | "accountant";

export interface Connection {
  id: string;
  name: string;
  kind: ConnectionKind;
  account: string;
  status: "healthy" | "stale";
  lastSyncedAt: ISODateTime;
  /** Optional pre-formatted label, e.g. "14:42 yesterday". */
  lastSyncedLabel?: string;
}

export interface HomeData {
  greeting: string;
  needsYouCount: number;
  dueSoon: DueItem[];
  currentMonth: { key: MonthKey; label: string; percentClosed: number };
  companies: CompanySummary[];
  handledPeriodLabel: string;
  handled: HandledStat[];
  connections: Connection[];
}

/* ---------- Needs you ---------- */

export interface DecisionOption {
  id: string;
  label: string;
  /** Optional one-tap follow-up choices, e.g. "Another company" → Company B / Company C. */
  choices?: { id: string; label: string }[];
}

export interface RememberRule {
  /** Template with {choice} placeholder, e.g. "Always use {choice} for IKEA paid with card •••• 4817". */
  template: string;
  /** Per-option override when {choice} would read awkwardly (e.g. "Personal"). */
  overrides?: Record<string, string>;
  defaultChecked: boolean;
}

export interface NeedsYouChoiceItem {
  id: string;
  kind: "choice";
  tone: Tone;
  eyebrow: string;
  merchant: string;
  amount: number;
  currency: string;
  date: ISODate;
  companyId?: string;
  paidWith?: string;
  question: string;
  options: DecisionOption[];
  why: string[];
  remember?: RememberRule;
}

export interface NeedsYouApprovalItem {
  id: string;
  kind: "approval";
  tone: Tone;
  eyebrow: string;
  merchant: string;
  title: string;
  amount: number;
  currency: string;
  date: ISODate;
  companyId?: string;
  body: string;
  facts: { label: string; value: string; tone?: Tone }[];
  why: string[];
  /** Step shown after "Confirm by phone": where to call and what to ask. */
  verification: {
    optionLabel: string;
    instruction: string;
    /** Checkbox the owner ticks after the call; required before release. */
    checkboxLabel: string;
    confirmLabel: string;
    confirmOptionId: string;
    confirmedMessage: string;
  };
  keepBlocked: { label: string; optionId: string; message: string };
}

export type NeedsYouItem = NeedsYouChoiceItem | NeedsYouApprovalItem;

export interface AnswerResult {
  ok: boolean;
  /** What the engine said, in plain language, when it has something to say. */
  message?: string;
}

/* ---------- Activity ---------- */

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
  at: ISODateTime;
  kind: ActivityKind;
  text: string;
  companyName?: string;
  amount?: number;
  currency?: string;
}

export interface ActivityFeed {
  /** The day treated as "Today" when grouping. Defaults to the real date. */
  today?: ISODate;
  items: ActivityItem[];
}

/* ---------- Companies & months ---------- */

export interface MatchedItem {
  id: string;
  supplier: string;
  description: string;
  amount: number;
  currency: string;
  date: ISODate;
  /** Plain-language reasons the match is right. */
  reasons: string[];
}

export interface RemainingItem {
  id: string;
  text: string;
  tone: Tone;
  href?: string;
  linkLabel?: string;
}

export interface MonthStats {
  transactionsChecked: number;
  documentsCollected: number;
  missingDocumentsRetrieved: number;
  suppliersChased: number;
  accountantQuestionsResolved: number;
  taxObligationsVerified: number;
  unresolvedIssues: number;
  minutesSpent: number;
}

export interface MonthClose {
  companyId: string;
  month: MonthKey;
  status: "closed" | "open";
  percentClosed: number;
  transactionsTotal: number;
  closedOn?: ISODate;
  stats: MonthStats;
  remaining: RemainingItem[];
  matched: MatchedItem[];
  notices?: RemainingItem[];
}

/* ---------- Ask ---------- */

export interface Evidence {
  label: string;
  id: string;
}

export interface AskAnswer {
  answer: string;
  evidence: Evidence[];
}

/* ---------- Audit ---------- */

export interface AuditFinding {
  id: string;
  value: string;
  label: string;
  tone: Tone;
  examples: string[];
}

export interface AuditResult {
  companyName: string;
  periodLabel: string;
  findings: AuditFinding[];
}

/* ---------- Onboarding ---------- */

export interface LearningCounter {
  id: string;
  source: string;
  value: number;
  label: string;
}

export interface OneTapQuestion {
  id: string;
  subject: string;
  amount: number;
  currency: string;
  cadence: string;
  options: { id: string; label: string }[];
}

export interface CompanyLookup {
  legalName: string;
  taxId: string;
  address: string;
  activity: string;
  registeredSince: string;
}

/* ---------- Accountant ---------- */

export interface AccountantClientRow {
  id: string;
  name: string;
  month: string;
  complete: number;
  missing: number;
  needsAccountant: number;
}

export interface AccountantClientDetail extends AccountantClientRow {
  taxId: string;
  software: string;
  evidence: { label: string; value: string }[];
  anomalies: { id: string; title: string; detail: string; tone: Tone }[];
  taxFlags: { id: string; title: string; detail: string }[];
  questions: { id: string; question: string; status: "answered" | "waiting"; answer?: string }[];
  exportState: {
    state: "ready" | "partial" | "exported";
    ready: number;
    total: number;
    note: string;
  };
}

/* ---------- Sources (GET /api/sources) ---------- */

export type SourceStatus = "healthy" | "stale" | "not_connected" | "known" | "hold";

export interface SourceItem {
  id: string;
  name: string;
  company: string;
  detail: string;
  status: SourceStatus;
  lastSyncedAt?: string | null;
  lastSeen?: string | null;
  foundIn?: string;
  renewsOn?: string | null;
}

export interface SourceGroup {
  id: string;
  title: string;
  description: string;
  items: SourceItem[];
}

export interface SourcesData {
  groups: SourceGroup[];
  companies: { id: string; name: string }[];
}
