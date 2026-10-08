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
  /** Production: what the EU VAT register says about the company, while the owner has not chosen yet. */
  identityCheck?: IdentityCheck;
}

/** The EU VAT register's details for a company, with a one-tap choice when they differ from what was typed. */
export interface IdentityCheck {
  status: string;
  /** "EU VAT register (VIES)" */
  source: string;
  vatNumber?: string | null;
  /** Plain words: "The EU VAT register lists this number as … Use these details?" */
  message: string;
  legalName?: string;
  address?: string;
  /** "Use these details" / "Keep what I typed": POST `{ use: true | false }` to `confirmPath`. */
  options?: { id: string; label: string }[];
  confirmPath?: string;
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
  /** Plain words about the problem, when it is stale. */
  message?: string;
  /** Production: what reconnecting takes, e.g. "Sign in to Gmail again". */
  action?: string;
  /**
   * Production: "catching_up" once the owner signed in again (or asked to try again). It stays
   * "stale" until a sync has actually worked; only then does it come back as "healthy".
   */
  reconnect?: "catching_up";
}

export interface HomeData {
  greeting: string;
  /** The engine's own status line ("Action required.", "I need 2 things from you."); it counts stale connections. */
  headline?: string;
  /** Open questions plus connections that need reconnecting. */
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

/** "Split it between several": amounts or percentages per cost center that must add up exactly. */
export interface SplitOffer {
  optionId: string;
  label: string;
  costCenters: { id: string; label: string }[];
  total: number;
  hint: string;
}

/** One part of a split answer: an amount or a percentage for one cost center. */
export interface SplitPart {
  costCenterId: string;
  amount?: string;
  percent?: string;
}

export interface NeedsYouChoiceItem {
  id: string;
  kind: "choice";
  tone: Tone;
  eyebrow: string;
  merchant: string;
  /** Null when nothing could be read yet (a photo to retake). */
  amount: number | null;
  currency: string;
  date: ISODate | null;
  companyId?: string;
  paidWith?: string;
  question: string;
  options: DecisionOption[];
  why: string[];
  remember?: RememberRule;
  /** "Which job is this for?" with more than one job: the payment can be split between them. */
  split?: SplitOffer;
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

/** A supplier's website sent the owner a one-time sign-in code: one box to enter it. */
export interface NeedsYouCodeItem {
  id: string;
  kind: "code";
  tone: Tone;
  eyebrow: string;
  /** The supplier: "Vodafone". */
  merchant: string;
  /** "Vodafone needs a sign-in code." */
  title: string;
  amount: null;
  currency: string;
  /** The day the website asked. */
  date: ISODate | null;
  companyId?: string;
  question: string;
  /** Where the code goes: POST `{ code }` to `submitPath` (`/api/portals/{connection}/code`). */
  code: {
    submitPath: string;
    label: string;
    /** "sms", "email", "app" … where the website sent it, when it said. */
    channel: string | null;
    /** Until when the code works, when the website said. */
    expiresAt: ISODateTime | null;
  };
  why: string[];
}

export type NeedsYouItem = NeedsYouChoiceItem | NeedsYouApprovalItem | NeedsYouCodeItem;

export interface AnswerResult {
  ok: boolean;
  /** What the engine said, in plain language, when it has something to say. */
  message?: string;
  /** The rule the engine learned from the answer ("Always use Hazel Tree for IKEA ..."), when it learned one. */
  learned?: string;
}

/** What a supplier's website said about the code the owner entered. */
export interface CodeResult {
  /** Signed in: the invoices are fetched and the question is closed. */
  done: boolean;
  /** Not done, but nothing went wrong: the website sent a new code, or asked for one more. */
  waiting: boolean;
  message: string;
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
  | "learned"
  /** An email written but not sent yet (no mailer accepted it). Never counted as handled. */
  | "waiting";

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
  /** What only the accountant can decide: tax flags and questions routed to them. */
  needsAccountant: number;
  /** Production: the client business the company belongs to (when the accountant may see all of it). */
  business?: string;
}

/** An original the accountant can open: `href` returns `{ filename, contentType, data }` (base64). */
export interface EvidenceLink {
  id: string;
  label: string;
  href: string;
  kind?: "payment" | "document" | "email" | "letter" | "file";
  sourceLabel?: string;
  receivedAt?: string;
  filename?: string | null;
}

/** One payment of the month and what proves it (§20, §54). */
export interface ReconciliationRow {
  id: string;
  date: ISODate;
  payee: string;
  description: string;
  amount: number;
  direction: "in" | "out";
  currency: string;
  status: "closed" | "not_required" | "conflict" | "waiting_for_owner" | "open";
  statusLabel: string;
  tone: Tone;
  documents: { id: string; label: string; href?: string; evidenceId?: string }[];
  evidence: EvidenceLink[];
  why: string[];
}

export interface AccountantClientDetail extends AccountantClientRow {
  taxId: string;
  software: string;
  evidence: { label: string; value: string }[];
  anomalies: { id: string; title: string; detail: string; tone: Tone }[];
  taxFlags: { id: string; title: string; detail: string }[];
  questions: {
    id: string;
    question: string;
    status: "answered" | "waiting";
    answer?: string;
    evidence?: EvidenceLink[];
  }[];
  exportState: {
    state: "ready" | "partial" | "exported";
    ready: number;
    total: number;
    note: string;
  };
  /** The month being prepared, the period the export covers. */
  period?: { key: MonthKey; from: string; to: string };
  /** The company's own accountant, else the business's. */
  accountant?: { name: string; firm: string; email: string } | null;
  evidenceLinks?: EvidenceLink[];
  reconciliation?: ReconciliationRow[];
  missingDocuments?: { id: string; date: ISODate; payee: string; amount: number | null; currency: string; plan: string }[];
  openReasons?: string[];
  /** Rules of this client's accountant that apply to it: "client" (this client) or "all" (all clients). */
  rules?: { id: string; label: string; scope: string }[];
  links?: { export: string; rules: string };
}

export interface AccountantInvitation {
  id: string;
  email: string;
  clientName?: string | null;
  taxIds: string[];
  createdAt: string;
  expiresAt: string;
  status: string;
  statusLabel: string;
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
  signIn?: string;
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

/* ---------- Diagram (GET /api/pipeline, backend/src/backoffice/pipeline.py) ---------- */

export interface PipelineStage {
  id: string;
  label: string;
  description: string;
  now: number;
  passed?: number;
}

export interface PipelineStep {
  stage: string;
  label: string;
  agent: string;
  agentLabel: string;
  at: string;
  note: string;
  evidence: number;
}

export interface PipelineItem {
  id: string;
  kind: "payment" | "document" | string;
  title: string;
  detail: string;
  amount: number | null;
  currency: string;
  date: string | null;
  company: string | null;
  stage: string;
  stageLabel: string;
  quality: "verified" | "likely" | "conflict" | string;
  open: boolean;
  source: string;
  reason: string;
  updatedAt: string | null;
  href?: string;
  journey: PipelineStep[];
}

export interface Pipeline {
  today: string;
  summary: {
    items: number;
    open: number;
    closed: number;
    waiting: number;
    conflicts: number;
    notRequired: number;
    openDeadlines: number;
    steps: number;
  };
  stages: PipelineStage[];
  side: PipelineStage[];
  sources: { id: string; label: string; count: number }[];
  agents: { id: string; label: string; description: string; count: number; unit: string }[];
  outputs: { id: string; label: string; items: { label: string; detail: string; tone: string; href?: string }[] }[];
  items: PipelineItem[];
}

/* ---------- Proof shared by the detail pages ---------- */

/** One original (a bank line, a document, an email): opened with GET /api/evidence/{id}/file. */
export interface EvidenceRef {
  id: string;
  label: string;
}

/** One step of a document's or payment's history. */
export interface HistoryStep {
  at: string;
  stage: string;
  label: string;
  note: string;
}

/** Invoice -> payment -> credit note -> refund, or deposit -> invoice -> each part -> held back. */
export interface ChainStep {
  step: string;
  id: string;
  label: string;
  date: ISODate | null;
  amount: number | null;
  currency: string;
  evidenceIds: string[];
}

export interface ImportChain {
  id: string;
  name: string;
  order: string | null;
  mrn: string | null;
  line: string;
  references: string[];
  pieces: { kind: string; id: string; role: string; label: string; date: ISODate | null; amount: number | null; currency: string; text: string }[];
}

/* ---------- Cost centers (GET /api/companies/{id}/cost-centers, GET /api/cost-centers/{id}) ---------- */

export interface CostCenterCard {
  id: string;
  companyId: string;
  name: string;
  /** The business's own word: "Job", "Property", "Vehicle", "Outlet", "Event", "Course", "Client". */
  kind: string;
  /** "Job Rua das Flores", "Apartment 2B". */
  label: string;
  active: boolean;
  identifiers: Record<string, string[]>;
  recharge: boolean;
  owner: string | null;
  managementFee: { percent: number | null; monthly: number | null } | null;
  isProperty: boolean;
}

export interface CostCenterRow extends CostCenterCard {
  spent: number;
  received: number;
  payments: number;
  documents: number;
  openItems: number;
  currency: string;
}

export interface Period {
  from: ISODate | null;
  to: ISODate | null;
  label: string;
}

export interface CostCentersData {
  companyId: string;
  companyName: string;
  kind: string;
  kindPlural: string;
  usesCostCenters: boolean;
  period: Period | null;
  costCenters: CostCenterRow[];
  general: { spent: number; received: number; payments: number };
  notDecided: { payments: number; amount: number; needsYouIds: string[] };
  headline: string;
}

export interface CostCenterPayment {
  id: string;
  date: ISODate;
  merchant: string;
  direction: "in" | "out";
  amount: number;
  total: number;
  currency: string;
  split: boolean;
  status: "closed" | "open";
  likely: boolean;
  why: string[];
  evidence: EvidenceRef[];
  toRecharge: boolean;
}

export interface CostCenterDocument {
  id: string;
  date: ISODate;
  supplier: string;
  label: string;
  amount: number;
  total: number;
  currency: string;
  split: boolean;
  paid: boolean;
  why: string[];
  evidence: EvidenceRef[];
}

export interface OpenItem {
  id: string;
  text: string;
  evidence: EvidenceRef[];
}

export interface CostCenterDetail extends CostCenterCard {
  companyName: string;
  period: Period | null;
  currency: string;
  spent: number;
  received: number;
  payments: CostCenterPayment[];
  documents: CostCenterDocument[];
  openItems: OpenItem[];
  evidence: EvidenceRef[];
  summary: string;
  recharged: { text: string; toRecharge: number; paidBack: number; outstanding: number; clientMoney: boolean } | null;
}

export interface CostCenterStatement {
  costCenter: CostCenterCard;
  companyName: string;
  title: string;
  owner: string | null;
  ownerStatement: boolean;
  period: Period | null;
  currency: string;
  moneyIn: (CostCenterPayment & { kind: string; label: string })[];
  received: number;
  costs: CostCenterPayment[];
  spent: number;
  managementFee: { amount: number; label: string; percent: number | null; monthly: number | null } | null;
  net: number;
  netDueToOwner: number | null;
  documents: CostCenterDocument[];
  openItems: OpenItem[];
  final: boolean;
  status: string;
  evidence: EvidenceRef[];
  summary: string;
}

/* ---------- People and expense claims (GET /api/employees, GET /api/expense-claims) ---------- */

export interface Employee {
  id: string;
  name: string;
  email: string | null;
  phone: string | null;
  companyId: string | null;
  companyName: string;
  cards: { last4: string; label: string }[];
  learnedFromBank: boolean;
  receiptsMissing: number;
  receiptsAsked: number;
  claimsWaiting: number;
  toPayBack: number | null;
}

export interface ExpenseClaim {
  id: string;
  merchant: string;
  amount: number | null;
  currency: string;
  date: ISODate;
  /** waiting (for the owner's OK), approved (to be paid back), paid (paid back), declined. */
  status: "waiting" | "approved" | "paid" | "declined" | string;
  statusLabel: string;
  employeeId: string;
  employee: string;
  companyId: string | null;
  companyName: string;
  evidenceIds: string[];
  paidBy: string | null;
  /** The open Needs You question that approves or declines it. */
  needsId: string | null;
  note: string;
}

/* ---------- Plan (GET /api/billing) ---------- */

export interface PlanLimits {
  companies: number | null;
  documentsPerMonth: number | null;
  users: number | null;
}

export interface BillingPlan {
  id: string;
  name: string;
  monthly: number;
  perClient: number | null;
  limits: PlanLimits;
  summary: string;
  price: string;
}

export interface BillingData {
  plan: BillingPlan & { status: string; renewsOn: ISODate | null };
  demo: boolean;
  usage: { month: MonthKey; companies: number; documents: number; users: number | null; clients: number };
  limits: PlanLimits;
  over: string[];
  prompt: string | null;
  notice: string | null;
  graceUntil: ISODate | null;
  waiting: number;
  held: boolean;
  plans: BillingPlan[];
  canUpgrade: boolean;
  canManage: boolean;
  message: string | null;
}

/* ---------- What I may do on my own (GET /api/settings/automation) ---------- */

export interface AutomationItem {
  id: string;
  label: string;
  detail: string;
  on: boolean;
  onFor: string[];
  companies: { companyId: string; companyName: string; on: boolean }[];
  level: string;
  levelLabel: string;
}

export interface AutomationData {
  items: AutomationItem[];
  summary: string;
  never: string;
  ok?: boolean;
  message?: string;
}

/* ---------- How I read your email and bank (GET /api/settings/reading) ---------- */

/** How far back the first read of a new mailbox or bank goes: the last 90 days or the last 12 months. */
export type HistoryChoice = "90d" | "12m";

export interface ReadingData {
  history: HistoryChoice;
  historyOptions: { id: HistoryChoice; label: string }[];
  historyLabel: string;
  historyDetail: string;
  lookInSpam: boolean;
  spamLabel: string;
  spamDetail: string;
  /** Mailboxes and banks reading their older months now (after choosing 12 months). */
  reading: string[];
  ok?: boolean;
  message?: string;
}

/* ---------- Deadlines (GET /api/obligations) ---------- */

export interface Obligation {
  id: string;
  title: string;
  kind: string;
  companyId: string | null;
  companyName: string;
  due: ISODate;
  amount: number | null;
  currency: string;
  reference: string;
  /** "You" or "Your accountant". */
  responsible: string;
  consequence: string;
  requiredProof: string;
  /** What proves it done, in plain words. */
  condition: string;
  status: "open" | "done" | "information";
  tone: Tone;
  nextStep: string;
  why: string[];
  evidenceIds: string[];
  /** How the owner can say it is done; empty when only a payment or a document can close it. */
  confirmOptions: { id: string; label: string; needsDate?: boolean }[];
}

export interface ObligationsData {
  today: ISODate;
  items: Obligation[];
}

/* ---------- One document (GET /api/documents/{id}) ---------- */

export interface StatementLine {
  row: number;
  status: string;
  text: string;
  evidence: EvidenceRef[];
  document?: { id: string; label: string };
  payment?: EvidenceRef;
}

export interface SupplierStatement {
  supplier: string;
  supplierKnown: boolean;
  note: string;
  summary: string;
  complete: boolean;
  lines?: StatementLine[];
  missing: StatementLine[];
  differences: StatementLine[];
  paymentsNotFound: StatementLine[];
  notOnStatement: { id: string; label: string; date: ISODate | null; amount: number | null; evidence: EvidenceRef[] }[];
  notOnStatementText?: string;
  balance: { statement: number | null; ours: number | null; difference: number | null; agrees: boolean; text: string } | null;
  request: { status: string; to?: string; text: string } | null;
  needsYouId: string | null;
}

export interface DocumentDetail {
  id: string;
  label: string;
  supplier: string;
  number: string;
  type: string;
  date: ISODate | null;
  amount: number | null;
  currency: string;
  companyId: string;
  companyName: string;
  /** The golden path stage: acquired, understood, verified, matched, confirmed, closed, conflict, needs_owner … */
  stage: string;
  statusLabel: string;
  /** Plain words when it is held (a large first purchase, a changed bank account). */
  waiting: string | null;
  corrects: { id: string; label: string } | null;
  creditNotes: { id: string; label: string }[];
  payments: (EvidenceRef & { transactionId: string })[];
  supportsPayments: EvidenceRef[];
  history: HistoryStep[];
  chain: ChainStep[];
  evidence: EvidenceRef[];
  evidenceIds: string[];
  why: string[];
  filename?: string;
  sensitive?: boolean;
  sensitiveReason?: string;
  due?: ISODate | null;
  dueLine?: string;
  parts?: ChainStep[];
  received?: { received: number; total: number; stillToCome: number; text: string };
  heldBack?: { amount: number; until: ISODate | null; status: string; text: string };
  importChain?: ImportChain;
  statement?: SupplierStatement;
}

/* ---------- One payment (GET /api/transactions/{id}) ---------- */

export interface TransactionDetail {
  id: string;
  date: ISODate;
  amount: number;
  currency: string;
  direction: "in" | "out";
  counterparty: string;
  companyId: string | null;
  companyName: string;
  status: string;
  statusLabel: string;
  /** What it needs to close: "EDP should send an invoice for this payment." */
  expects: string;
  documents: { id: string; label: string; evidenceIds: string[] }[];
  headline: string;
  why: string[];
  history: HistoryStep[];
  chain: ChainStep[];
  evidenceIds: string[];
  nextStep?: string;
  /** One tap teaches it for every later payment: "It never has an invoice" / "It always has one". */
  evidenceChoices?: { need: "none" | "invoice"; label: string }[];
  notes?: string[];
  parts?: ChainStep[];
  deposit?: { status: string; text: string };
  disputed?: { status: string; text: string };
  importChain?: ImportChain;
}
