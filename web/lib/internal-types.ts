/**
 * Shapes of the internal dashboard's data ("Admin OS", /internal), exactly as
 * backend/src/backoffice/internal.py returns them.
 */
import type { PipelineStage } from "./types";

export interface GoldenTotal {
  id: "zero_touch" | "recovered" | "unresolved" | "owner_minutes" | string;
  label: string;
  target: number;
  targetLabel: string;
  hasData: boolean;
  value: number | null;
  /** 0–100: what the ring and the bar show. */
  ring: number;
  ringLabel: string;
  ringCaption: string;
  count: number;
  countLabel: string;
  onTarget: boolean | null;
  remaining: number;
  remainingLabel: string;
  detail: string;
  estimate: boolean;
}

export interface HealthPart {
  id: string;
  label: string;
  score: number | null;
  detail: string;
}

export interface Health {
  score: number | null;
  tone: "good" | "attention" | "risk" | "neutral";
  parts: HealthPart[];
}

export interface CriticalFix {
  id: string;
  severity: "red" | "amber" | "blue";
  label: string;
  detail: string;
  tenant: string;
  company: string | null;
  href: string | null;
}

export interface TenantCompany {
  id: string;
  name: string;
  legalName: string;
  taxId: string;
  tone: "good" | "attention" | "risk" | string;
  statusLabel: string;
  percentClosed: number;
  itemsDone: number;
  itemsTotal: number;
  needsYou: number;
  missingDocuments: number;
  closedOn: string | null;
}

export interface TenantRow {
  id: string;
  owner: string;
  email: string;
  month: string;
  monthLabel: string;
  companies: TenantCompany[];
  items: number;
  openItems: number;
  needsYou: number;
  connections: { healthy: number; total: number };
  audit: { records: number; intact: boolean };
}

export interface InternalConnection {
  id: string;
  tenant: string;
  name: string;
  kind: "email" | "bank" | "accountant" | string;
  account: string;
  status: "healthy" | "stale" | string;
  companies: string[];
  lastSyncedAt: string | null;
  coveredFrom: string | null;
  coveredUntil: string | null;
  message: string | null;
  signIn: string;
}

export interface TargetRow {
  id: string;
  label: string;
  value: number | null;
  display: string;
  target: string;
  onTarget: boolean | null;
  estimate: boolean;
  evidence: string;
  definition: string;
}

export interface ReadinessItem {
  id: string;
  title: string;
  status: "live" | "pending";
  percent: number;
  detail: string;
  area: string;
}

export interface Readiness {
  items: ReadinessItem[];
  live: number;
  pending: number;
  total: number;
  percent: number;
}

/** Background work that failed every attempt and is parked for the team (server/jobs.py), never dropped. */
export interface DeadLetter {
  id: string;
  tenant: string;
  /** "sync.connection", "subscription.renew" … */
  kind: string;
  /** The job in words: "Reading a mailbox after a push notification". */
  label: string;
  /** The connection it was for, when there is one. */
  connection: string;
  attempts: number;
  /** Why the last attempt failed. */
  lastError: string;
  /** When it was parked. */
  since: string | null;
}

export interface InternalOverview {
  generatedAt: string;
  today: string;
  period: { key: string; label: string };
  golden: GoldenTotal[];
  health: Health;
  fixes: CriticalFix[];
  pipeline: {
    summary: Record<string, number>;
    stages: PipelineStage[];
    side: PipelineStage[];
    agents: { id: string; label: string; description: string; count: number; unit: string }[];
  };
  tenants: TenantRow[];
  connections: InternalConnection[];
  targets: TargetRow[];
  readiness: Readiness;
  quick: { id: string; label: string; value: number }[];
  /** The production server's parked jobs (absent where there is no job queue, e.g. the in-browser engine). */
  deadLetters?: DeadLetter[];
}

export interface OperationsActivity {
  id: string;
  at: string;
  kind: string;
  text: string;
  tenant: string;
  company: string | null;
  evidence: number;
  amount?: number;
  currency?: string;
}

export interface AuditEntry {
  id: string;
  tenant: string;
  seq: number;
  at: string;
  actor: string;
  agent: string;
  action: string;
  subject: string | null;
  evidence: number;
  model: string | null;
  parser: string | null;
  summary: string;
  hash: string;
}

export interface InternalOperations {
  generatedAt: string;
  today: string;
  activity: OperationsActivity[];
  audit: {
    records: number;
    intact: boolean;
    chains: { tenant: string; records: number; intact: boolean; head: string; problem: string | null; detail: string }[];
    agents: { id: string; count: number }[];
    shown: number;
    entries: AuditEntry[];
  };
}

/* ---------- QA: the 50 SME cases and the acceptance checklist (backend/src/backoffice/acceptance.py) ---------- */

export type AcceptanceStatus = "pass" | "partial" | "missing";

export interface AcceptanceCounts {
  pass: number;
  partial: number;
  missing: number;
  total: number;
}

export interface AcceptanceCheck {
  id: string;
  text: string;
  status: AcceptanceStatus;
  where: string;
  note: string;
  evidence: string[];
}

export interface AcceptanceSection {
  id: string;
  title: string;
  counts: AcceptanceCounts;
  checks: AcceptanceCheck[];
}

export interface AcceptanceCase {
  number: number;
  title: string;
  quote: string;
  verdict: AcceptanceStatus;
  passTest: { text: string; status: AcceptanceStatus; checks: { id: string; text: string; status: AcceptanceStatus }[] };
  handles: { text: string; check: string; status: AcceptanceStatus; where: string; note: string }[];
  covered: number;
  handled: number;
  gaps: { id: string; text: string; status: AcceptanceStatus; note: string }[];
}

export interface InternalAcceptance {
  purpose: string;
  legend: Record<AcceptanceStatus, string>;
  summary: {
    cases: AcceptanceCounts;
    passTests: AcceptanceCounts;
    checklist: AcceptanceCounts;
    percent: number;
    sector: AcceptanceCounts;
  };
  sections: AcceptanceSection[];
  sector: AcceptanceCheck[];
  cases: AcceptanceCase[];
  finalTest: { question: string; categories: string[]; rule: string; status: AcceptanceStatus; note: string };
}
