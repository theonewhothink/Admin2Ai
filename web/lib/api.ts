/**
 * Data access. Every call tries the backend at NEXT_PUBLIC_API_URL first and
 * falls back to the sample data in ./data.ts when the variable is unset, the
 * request fails, times out, or returns something unexpected. Pages therefore
 * always render, with or without a backend.
 *
 * Safe to import from both Server and Client Components.
 */
import { unstable_rethrow } from "next/navigation";
import * as sample from "./data";
import type {
  ActivityFeed,
  AnswerResult,
  AskAnswer,
  CompanySummary,
  HomeData,
  MonthClose,
  MonthKey,
  NeedsYouItem,
} from "./types";

const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/+$/, "");
const TIMEOUT_MS = 4000;

/** True when a backend URL is configured. */
export const hasApi = API_URL !== "";

type Guard<T> = (value: unknown) => T | null;

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Accept either a bare array or `{ [key]: [...] }`. */
function arrayFrom(v: unknown, key: string): unknown[] | null {
  if (Array.isArray(v)) return v;
  if (isRecord(v) && Array.isArray(v[key])) return v[key] as unknown[];
  return null;
}

function warn(path: string, reason: unknown) {
  if (process.env.NODE_ENV !== "production") {
    const message = reason instanceof Error ? reason.message : String(reason);
    console.warn(`[api] ${path}: using sample data (${message})`);
  }
}

async function request<T>(path: string, init: RequestInit, guard: Guard<T>, fallback: () => T): Promise<T> {
  if (!hasApi) return fallback();
  try {
    const res = await fetch(`${API_URL}${path}`, {
      cache: "no-store",
      ...init,
      headers: { Accept: "application/json", ...(init.headers ?? {}) },
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const text = await res.text();
    const parsed: unknown = text ? JSON.parse(text) : {};
    const value = guard(parsed);
    if (value === null) throw new Error("unexpected response shape");
    return value;
  } catch (err) {
    // Let Next.js handle its own control-flow signals (dynamic rendering, redirects).
    unstable_rethrow(err);
    warn(path, err);
    return fallback();
  }
}

/* ---------- Reads ---------- */

export function getHome(): Promise<HomeData> {
  return request<HomeData>(
    "/api/home",
    { method: "GET" },
    (v) => {
      if (!isRecord(v) || typeof v.greeting !== "string" || !Array.isArray(v.companies) || !Array.isArray(v.handled)) {
        return null;
      }
      const d = v as unknown as HomeData;
      return {
        ...d,
        dueSoon: Array.isArray(d.dueSoon) ? d.dueSoon : [],
        connections: Array.isArray(d.connections) ? d.connections : [],
        handledPeriodLabel: d.handledPeriodLabel ?? "This week",
      };
    },
    () => sample.home,
  );
}

export function getNeedsYou(): Promise<NeedsYouItem[]> {
  return request<NeedsYouItem[]>(
    "/api/needs-you",
    { method: "GET" },
    (v) => {
      const list = arrayFrom(v, "items");
      if (!list) return null;
      return list.filter(
        (i): i is NeedsYouItem => isRecord(i) && typeof i.id === "string" && (i.kind === "choice" || i.kind === "approval"),
      );
    },
    () => sample.needsYou,
  );
}

export function getActivity(): Promise<ActivityFeed> {
  return request<ActivityFeed>(
    "/api/activity",
    { method: "GET" },
    (v) => {
      const list = arrayFrom(v, "items");
      if (!list) return null;
      const today = isRecord(v) && typeof v.today === "string" ? v.today : undefined;
      return { today, items: list as ActivityFeed["items"] };
    },
    () => sample.activity,
  );
}

export function getCompanies(): Promise<CompanySummary[]> {
  return request<CompanySummary[]>(
    "/api/companies",
    { method: "GET" },
    (v) => {
      const list = arrayFrom(v, "companies");
      if (!list) return null;
      return list.filter((c): c is CompanySummary => isRecord(c) && typeof c.id === "string" && typeof c.name === "string");
    },
    () => sample.companies,
  );
}

export async function getCompany(id: string): Promise<CompanySummary | null> {
  const list = await getCompanies();
  return list.find((c) => c.id === id) ?? null;
}

export function getMonth(companyId: string, month: MonthKey): Promise<MonthClose | null> {
  return request<MonthClose | null>(
    `/api/months/${encodeURIComponent(companyId)}/${encodeURIComponent(month)}`,
    { method: "GET" },
    (v) => (isRecord(v) && typeof v.month === "string" && (v.status === "open" || v.status === "closed") ? (v as unknown as MonthClose) : null),
    () => sample.findMonth(companyId, month),
  );
}

/* ---------- Writes (called from the browser) ---------- */

const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Record the owner's answer to a needs-you item.
 * Phase 0: without a reachable backend the answer is accepted locally.
 */
export async function answerNeedsYou(id: string, optionId: string, remember: boolean): Promise<AnswerResult> {
  if (!hasApi) {
    await pause(450);
    return { ok: true };
  }
  return request<AnswerResult>(
    `/api/needs-you/${encodeURIComponent(id)}/answer`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ option_id: optionId, remember }),
    },
    () => ({ ok: true }),
    () => ({ ok: true }),
  );
}

function sampleAnswer(question: string): AskAnswer {
  const q = question.trim().toLowerCase().replace(/[?.!]+$/, "");
  const exact = Object.entries(sample.askAnswers).find(([k]) => k.toLowerCase().replace(/[?.!]+$/, "") === q);
  if (exact) return exact[1];
  const pick = (key: string) => sample.askAnswers[key] ?? sample.askFallback;
  if (q.includes("vodafone")) return pick("Did we pay Vodafone?");
  if (q.includes("800") || (q.includes("invoice") && q.includes("yesterday"))) {
    return pick("Find the invoice for the €800 payment yesterday.");
  }
  if (q.includes("accountant")) return pick("What did the accountant ask this month?");
  if (q.includes("subscription") || q.includes("increase") || q.includes("went up")) {
    return pick("Show subscriptions that increased.");
  }
  if (q.includes("attention") || q.includes("need") || q.includes("todo") || q.includes("to do")) {
    return pick("What still needs my attention?");
  }
  if (q.includes("september") || q.includes("complete") || q.includes("closed")) {
    return pick("Is September complete?");
  }
  return sample.askFallback;
}

export async function ask(question: string): Promise<AskAnswer> {
  if (!hasApi) {
    await pause(700);
    return sampleAnswer(question);
  }
  return request<AskAnswer>(
    "/api/ask",
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question }),
    },
    (v) =>
      isRecord(v) && typeof v.answer === "string"
        ? { answer: v.answer, evidence: Array.isArray(v.evidence) ? (v.evidence as AskAnswer["evidence"]) : [] }
        : null,
    () => sampleAnswer(question),
  );
}

/** Upload a receipt or invoice. Phase 0: accepted locally without a backend. */
export async function uploadEvidence(file: File): Promise<AnswerResult> {
  if (!hasApi) {
    await pause(600 + Math.min(file.size / 2000, 900));
    return { ok: true };
  }
  const body = new FormData();
  body.append("file", file);
  return request<AnswerResult>("/api/evidence", { method: "POST", body }, () => ({ ok: true }), () => ({ ok: true }));
}

/* ---------- Sample-only (no endpoint yet) ---------- */

export const getAudit = async () => sample.audit;
export const getAccountantClients = async () => sample.accountantClients;
export const getAccountantClient = async (id: string) => sample.accountantClientDetail(id);
