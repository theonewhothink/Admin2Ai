/**
 * Validate API responses before they reach the screen. Anything malformed is
 * dropped (one item at a time) rather than shown half-broken (§70: no raw errors).
 */
import { toDecimal } from "../lib/money";
import { isRecord } from "./http";
import type {
  ActivityFeed,
  ActivityItem,
  ActivityKind,
  AskAnswer,
  CompanySummary,
  Connection,
  DecisionOption,
  HandledStat,
  HomeData,
  NeedsYouApprovalItem,
  NeedsYouChoiceItem,
  NeedsYouItem,
  RememberRule,
  Tone,
} from "./types";

const TONES: readonly Tone[] = ["good", "attention", "risk", "neutral"];
const ACTIVITY_KINDS: readonly ActivityKind[] = [
  "collected", "recovered", "chased", "answered", "checked", "closed", "protected", "learned",
];

const str = (v: unknown): v is string => typeof v === "string";
const nonEmpty = (v: unknown): v is string => typeof v === "string" && v.trim().length > 0;
const tone = (v: unknown): Tone => (TONES.includes(v as Tone) ? (v as Tone) : "neutral");
const strings = (v: unknown): string[] => (Array.isArray(v) ? v.filter(str) : []);
const count = (v: unknown): number | null =>
  typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : null;

/** Accept a bare array or `{ [key]: [...] }`, like the web client. */
export function arrayFrom(v: unknown, key: string): unknown[] | null {
  if (Array.isArray(v)) return v;
  if (isRecord(v) && Array.isArray(v[key])) return v[key] as unknown[];
  return null;
}

function compact<T>(values: Array<T | null>): T[] {
  return values.filter((v): v is T => v !== null);
}

function company(v: unknown): CompanySummary | null {
  if (!isRecord(v) || !nonEmpty(v.id) || !nonEmpty(v.name)) return null;
  const out: CompanySummary = {
    id: v.id,
    name: v.name,
    tone: tone(v.tone),
    statusLabel: str(v.statusLabel) ? v.statusLabel : "",
    detail: str(v.detail) ? v.detail : "",
  };
  const pending = strings(v.pendingItemIds);
  if (pending.length) out.pendingItemIds = pending;
  return out;
}

function connection(v: unknown): Connection | null {
  if (!isRecord(v) || !nonEmpty(v.id) || !nonEmpty(v.name) || !str(v.lastSyncedAt)) return null;
  if (v.kind !== "email" && v.kind !== "bank" && v.kind !== "accountant") return null;
  const out: Connection = {
    id: v.id,
    name: v.name,
    kind: v.kind,
    account: str(v.account) ? v.account : "",
    // Unknown status is treated as stale: never show green on doubt (§47-48).
    status: v.status === "healthy" ? "healthy" : "stale",
    lastSyncedAt: v.lastSyncedAt,
  };
  if (str(v.lastSyncedLabel)) out.lastSyncedLabel = v.lastSyncedLabel;
  return out;
}

function handled(v: unknown): HandledStat | null {
  if (!isRecord(v) || !nonEmpty(v.id) || !str(v.label)) return null;
  const c = count(v.count);
  return c === null ? null : { id: v.id, count: c, label: v.label };
}

export function parseHome(v: unknown): HomeData | null {
  if (!isRecord(v) || !Array.isArray(v.companies)) return null;
  const needs = count(v.needsYouCount);
  const month = isRecord(v.currentMonth) ? v.currentMonth : null;
  const pct = month && typeof month.percentClosed === "number" ? month.percentClosed : null;
  if (needs === null || !month || !str(month.key) || !str(month.label) || pct === null || pct < 0 || pct > 100) {
    return null;
  }
  const out: HomeData = {
    needsYouCount: needs,
    currentMonth: { key: month.key, label: month.label, percentClosed: pct },
    companies: compact(v.companies.map(company)),
    handled: Array.isArray(v.handled) ? compact(v.handled.map(handled)) : [],
    connections: Array.isArray(v.connections) ? compact(v.connections.map(connection)) : [],
  };
  if (str(v.greeting)) out.greeting = v.greeting;
  if (str(v.handledPeriodLabel)) out.handledPeriodLabel = v.handledPeriodLabel;
  const today = count(v.handledToday);
  if (today !== null) out.handledToday = today;
  return out;
}

function option(v: unknown): DecisionOption | null {
  if (!isRecord(v) || !nonEmpty(v.id) || !nonEmpty(v.label)) return null;
  const out: DecisionOption = { id: v.id, label: v.label };
  if (Array.isArray(v.choices)) {
    const choices = compact(
      v.choices.map((c) => (isRecord(c) && nonEmpty(c.id) && nonEmpty(c.label) ? { id: c.id, label: c.label } : null)),
    );
    if (choices.length) out.choices = choices;
  }
  return out;
}

function remember(v: unknown): RememberRule | undefined {
  if (!isRecord(v) || !str(v.template)) return undefined;
  const out: RememberRule = { template: v.template, defaultChecked: v.defaultChecked === true };
  if (isRecord(v.overrides)) {
    const entries = Object.entries(v.overrides).filter((e): e is [string, string] => str(e[1]));
    if (entries.length) out.overrides = Object.fromEntries(entries);
  }
  return out;
}

function needsBase(v: Record<string, unknown>) {
  if (!nonEmpty(v.id) || !str(v.merchant) || !str(v.date)) return null;
  const base = {
    id: v.id,
    tone: tone(v.tone),
    eyebrow: str(v.eyebrow) ? v.eyebrow : "",
    merchant: v.merchant,
    amount: toDecimal(v.amount),
    currency: nonEmpty(v.currency) ? v.currency : "EUR",
    date: v.date,
    why: strings(v.why),
  };
  return nonEmpty(v.companyId) ? { ...base, companyId: v.companyId } : base;
}

function choiceItem(v: Record<string, unknown>): NeedsYouChoiceItem | null {
  const base = needsBase(v);
  if (!base || !str(v.question) || !Array.isArray(v.options)) return null;
  const options = compact(v.options.map(option));
  if (options.length === 0) return null;
  const out: NeedsYouChoiceItem = { ...base, kind: "choice", question: v.question, options };
  if (str(v.paidWith)) out.paidWith = v.paidWith;
  const rule = remember(v.remember);
  if (rule) out.remember = rule;
  return out;
}

function approvalItem(v: Record<string, unknown>): NeedsYouApprovalItem | null {
  const base = needsBase(v);
  const ver = isRecord(v.verification) ? v.verification : null;
  const keep = isRecord(v.keepBlocked) ? v.keepBlocked : null;
  if (!base || !str(v.title) || !str(v.body) || !ver || !keep) return null;
  const verKeys = ["optionLabel", "instruction", "checkboxLabel", "confirmLabel", "confirmOptionId", "confirmedMessage"] as const;
  if (!verKeys.every((k) => str(ver[k])) || !nonEmpty(ver.confirmOptionId)) return null;
  if (!str(keep.label) || !nonEmpty(keep.optionId) || !str(keep.message)) return null;
  const facts = Array.isArray(v.facts)
    ? compact(
        v.facts.map((f) =>
          isRecord(f) && str(f.label) && str(f.value)
            ? { label: f.label, value: f.value, ...(f.tone ? { tone: tone(f.tone) } : {}) }
            : null,
        ),
      )
    : [];
  return {
    ...base,
    kind: "approval",
    title: v.title,
    body: v.body,
    facts,
    verification: {
      optionLabel: ver.optionLabel as string,
      instruction: ver.instruction as string,
      checkboxLabel: ver.checkboxLabel as string,
      confirmLabel: ver.confirmLabel as string,
      confirmOptionId: ver.confirmOptionId as string,
      confirmedMessage: ver.confirmedMessage as string,
    },
    keepBlocked: { label: keep.label, optionId: keep.optionId, message: keep.message },
  };
}

export function parseNeedsYou(v: unknown): NeedsYouItem[] | null {
  const list = arrayFrom(v, "items");
  if (!list) return null;
  return compact(
    list.map((item) => {
      if (!isRecord(item)) return null;
      if (item.kind === "choice") return choiceItem(item);
      if (item.kind === "approval") return approvalItem(item);
      return null;
    }),
  );
}

function activityItem(v: unknown): ActivityItem | null {
  if (!isRecord(v) || !nonEmpty(v.id) || !str(v.at) || !str(v.text)) return null;
  if (!ACTIVITY_KINDS.includes(v.kind as ActivityKind)) return null;
  const out: ActivityItem = { id: v.id, at: v.at, kind: v.kind as ActivityKind, text: v.text };
  if (str(v.companyName)) out.companyName = v.companyName;
  const amount = toDecimal(v.amount);
  if (amount !== null) {
    out.amount = amount;
    out.currency = nonEmpty(v.currency) ? v.currency : "EUR";
  }
  return out;
}

export function parseActivity(v: unknown): ActivityFeed | null {
  const list = arrayFrom(v, "items");
  if (!list) return null;
  const feed: ActivityFeed = { items: compact(list.map(activityItem)) };
  if (isRecord(v) && str(v.today)) feed.today = v.today;
  return feed;
}

export function parseAskAnswer(v: unknown): AskAnswer | null {
  if (!isRecord(v) || !nonEmpty(v.answer)) return null;
  const evidence = Array.isArray(v.evidence)
    ? compact(v.evidence.map((e) => (isRecord(e) && nonEmpty(e.label) && nonEmpty(e.id) ? { label: e.label, id: e.id } : null)))
    : [];
  return { answer: v.answer, evidence };
}
