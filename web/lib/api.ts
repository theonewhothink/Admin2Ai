/**
 * Data access. The mode is decided in ./mode.ts:
 *
 * - demo (NEXT_PUBLIC_ENGINE=browser): every call goes to the real Python
 *   engine running in the browser (./engine.ts). The static GitHub Pages site.
 * - production (NEXT_PUBLIC_API_URL + NEXT_PUBLIC_REQUIRE_SIGNIN=1): every call
 *   goes to the backend from the browser with the session cookie
 *   (credentials: "include"), and state-changing calls carry the CSRF header
 *   `X-Requested-With: admin2ai`. A 401 sends the owner to /signin?next=….
 *   Nothing ever falls back to sample data: a failed read throws ApiError
 *   (the page shows "This page didn't load."), a failed write says so.
 * - api (NEXT_PUBLIC_API_URL only): the backend over HTTP, no sign-in.
 * - sample: the sample data in ./data.ts.
 *
 * Outside production, a call that fails, times out, or returns something
 * unexpected falls back to the sample data, so the demo always renders.
 *
 * Safe to import from both Server and Client Components.
 */
import { unstable_rethrow } from "next/navigation";
import * as sample from "./data";
import { engineRequest } from "./engine";
import type { InternalAcceptance, InternalOperations, InternalOverview } from "./internal-types";
import { API_URL, BASE_PATH, browserEngine, clientRendered, production } from "./mode";
import type {
  AccountantClientDetail,
  AccountantClientRow,
  AccountantInvitation,
  ActivityFeed,
  AnswerResult,
  AskAnswer,
  AuditResult,
  CompanySummary,
  HomeData,
  MonthClose,
  MonthKey,
  NeedsYouItem,
  Pipeline,
  SourcesData,
  SplitPart,
} from "./types";

const TIMEOUT_MS = 4000;
/** Production reads can wait a little longer than the demo's quick fallback. */
const PRODUCTION_TIMEOUT_MS = 15000;

/** True when a backend URL is configured. */
export const hasApi = API_URL !== "";

/** True when pages show data computed by the engine (over HTTP or in the browser), not sample data. */
export const liveData = hasApi || browserEngine;

export { browserEngine, clientRendered, production };

type Guard<T> = (value: unknown) => T | null;

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/* ---------- Production transport: session cookie, CSRF header, 401 → sign in ---------- */

/** A production call that could not give an answer. `message` is plain language, safe to show. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export const OFFLINE_MESSAGE = "I couldn’t reach the server. Check your connection and try again.";
const SERVER_MESSAGE = "Something went wrong on our side. Try again in a moment.";

/**
 * One request to the backend. In production it sends the session cookie
 * (credentials: "include") and, on anything but GET/HEAD, the CSRF header the
 * API requires for cookie sessions. Rejects only when the network fails.
 */
export function apiFetch(path: string, init: RequestInit = {}, timeoutMs = production ? PRODUCTION_TIMEOUT_MS : TIMEOUT_MS): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers: Record<string, string> = { Accept: "application/json", ...((init.headers as Record<string, string> | undefined) ?? {}) };
  if (production && method !== "GET" && method !== "HEAD") headers["X-Requested-With"] = "admin2ai";
  return fetch(`${API_URL}${path}`, {
    cache: "no-store",
    ...init,
    method,
    headers,
    ...(production ? { credentials: "include" as const } : {}),
    signal: AbortSignal.timeout(timeoutMs),
  });
}

/** The sign-in page, coming back to `next` afterwards. */
export function signInHref(next?: string): string {
  const back = next && next !== "/" ? `?next=${encodeURIComponent(next)}` : "";
  return `/signin${back}`;
}

/** The current in-app location (without the base path), for ?next=. */
export function currentPath(): string {
  if (typeof window === "undefined") return "/";
  const { pathname, search, hash } = window.location;
  const path = BASE_PATH && pathname.startsWith(BASE_PATH) ? pathname.slice(BASE_PATH.length) || "/" : pathname;
  return `${path}${search}${hash}`;
}

/**
 * The session is missing or expired: go to sign-in and come back here after.
 * Never resolves in the browser (the page is leaving), so no half-loaded
 * screen flashes; on the server it throws.
 */
export function toSignIn(): Promise<never> {
  if (typeof window === "undefined") throw new ApiError("Sign in to continue.", 401);
  const here = currentPath();
  // Called from data loaders, outside React: a full page load also drops everything the expired session loaded.
  // eslint-disable-next-line @next/next/no-location-assign-relative-destination
  if (!/^\/sign(in|up)(\/|\?|#|$)/.test(here)) window.location.assign(`${BASE_PATH}${signInHref(here)}`);
  return new Promise<never>(() => undefined);
}

/** The server's own plain-language message, or one of ours by status. Never a stack trace. */
export async function errorMessage(res: Response, fallback = SERVER_MESSAGE): Promise<string> {
  let message: unknown;
  try {
    const body: unknown = await res.clone().json();
    message = isRecord(body) ? body.message : undefined;
  } catch {
    message = undefined;
  }
  if (typeof message === "string" && message.trim() && message.length <= 300 && !/traceback|exception|\n\s+at\s/i.test(message)) {
    return message.trim();
  }
  if (res.status === 429) return "Too many attempts. Wait a few minutes, then try again.";
  if (res.status >= 500) return SERVER_MESSAGE;
  return fallback;
}

/** A production read: data, or ApiError. 401 goes to sign-in. */
async function productionRead<T>(path: string, init: RequestInit, guard: Guard<T>, notFound?: { value: T }): Promise<T> {
  let res: Response;
  try {
    res = await apiFetch(path, init);
  } catch {
    throw new ApiError(OFFLINE_MESSAGE, 0);
  }
  if (res.status === 401) return toSignIn();
  if (res.status === 404 && notFound) return notFound.value;
  if (!res.ok) throw new ApiError(await errorMessage(res), res.status);
  let parsed: unknown;
  try {
    const text = await res.text();
    parsed = text ? JSON.parse(text) : {};
  } catch {
    throw new ApiError(SERVER_MESSAGE, res.status);
  }
  const value = guard(parsed);
  if (value === null) throw new ApiError(SERVER_MESSAGE, res.status);
  return value;
}

/** A production write: `{ ok, message }`, never pretending. 401 goes to sign-in. */
async function productionWrite(
  path: string,
  init: RequestInit,
  timeoutMs?: number,
): Promise<AnswerResult & { body: Record<string, unknown> }> {
  let res: Response;
  try {
    res = await apiFetch(path, init, timeoutMs);
  } catch {
    return { ok: false, message: OFFLINE_MESSAGE, body: {} };
  }
  if (res.status === 401) return toSignIn();
  const parsed: unknown = await res
    .clone()
    .json()
    .catch(() => ({}));
  const body = isRecord(parsed) ? parsed : {};
  if (!res.ok) return { ok: false, message: await errorMessage(res, "I couldn’t save that. Try again."), body };
  return { ok: true, message: typeof body.message === "string" ? body.message : undefined, body };
}

/** Accept either a bare array or `{ [key]: [...] }`. */
function arrayFrom(v: unknown, key: string): unknown[] | null {
  if (Array.isArray(v)) return v;
  if (isRecord(v) && Array.isArray(v[key])) return v[key] as unknown[];
  return null;
}

function warn(path: string, reason: unknown) {
  if (process.env.NODE_ENV !== "production" || browserEngine) {
    const message = reason instanceof Error ? reason.message : String(reason);
    console.warn(`[api] ${path}: using sample data (${message})`);
  }
}

/** Ask the in-browser engine. On the server (static build time) there is no engine: use the sample. */
async function viaEngine<T>(
  method: string,
  path: string,
  body: unknown,
  guard: Guard<T>,
  fallback: () => T,
  notFound?: { value: T },
): Promise<T> {
  if (typeof window === "undefined") return fallback();
  try {
    const reply = await engineRequest(method, path, body);
    // The engine knows its own data: "not found" is an answer, not a reason to show sample data.
    if (reply.status === 404 && notFound) return notFound.value;
    if (reply.status !== 200) throw new Error(`HTTP ${reply.status}`);
    const value = guard(reply.body);
    if (value === null) throw new Error("unexpected response shape");
    return value;
  } catch (err) {
    warn(path, err);
    return fallback();
  }
}

async function request<T>(
  path: string,
  init: RequestInit,
  guard: Guard<T>,
  fallback: () => T,
  engineBody?: unknown,
  engineNotFound?: { value: T },
): Promise<T> {
  if (browserEngine) return viaEngine(init.method ?? "GET", path, engineBody, guard, fallback, engineNotFound);
  if (production) return productionRead(path, init, guard, engineNotFound);
  if (!hasApi) return fallback();
  try {
    const res = await apiFetch(path, init);
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
    undefined,
    { value: null },
  );
}

/* ---------- Writes (called from the browser) ---------- */

const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** A write to the in-browser engine. `ok` is false when the engine refused it; `message` says why. */
async function engineWrite(path: string, body: unknown): Promise<AnswerResult> {
  try {
    const reply = await engineRequest("POST", path, body);
    const message = isRecord(reply.body) && typeof reply.body.message === "string" ? reply.body.message : undefined;
    const learned = isRecord(reply.body) && typeof reply.body.learned === "string" ? reply.body.learned : undefined;
    return { ok: reply.status === 200, message, ...(learned ? { learned } : {}) };
  } catch (err) {
    warn(path, err);
    return { ok: false };
  }
}

async function toBase64(file: Blob): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}

/** A file as the JSON upload routes take it (expense claims, a deadline's proof). */
export async function filePayload(file: File): Promise<{ filename: string; contentType: string | null; dataBase64: string }> {
  return { filename: file.name, contentType: file.type || null, dataBase64: await toBase64(file) };
}

/**
 * Record the owner's answer to a needs-you item. `split` is the "Split it between several" answer:
 * an amount or a percentage per cost center, which must add up exactly (the engine says so plainly
 * when they don't).
 * Phase 0: without a reachable backend the answer is accepted locally.
 */
export async function answerNeedsYou(id: string, optionId: string, remember: boolean, split?: SplitPart[]): Promise<AnswerResult> {
  const answer = { option_id: optionId, remember, ...(split ? { split } : {}) };
  if (browserEngine) {
    return engineWrite(`/api/needs-you/${encodeURIComponent(id)}/answer`, answer);
  }
  if (production) {
    const { ok, message, body } = await productionWrite(`/api/needs-you/${encodeURIComponent(id)}/answer`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(answer),
    });
    return { ok, message, ...(ok && typeof body.learned === "string" ? { learned: body.learned } : {}) };
  }
  if (!hasApi) {
    await pause(450);
    return { ok: true };
  }
  return request<AnswerResult>(
    `/api/needs-you/${encodeURIComponent(id)}/answer`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(answer),
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
  if (production) {
    try {
      return await productionRead<AskAnswer>(
        "/api/ask",
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question }) },
        (v) =>
          isRecord(v) && typeof v.answer === "string"
            ? { answer: v.answer, evidence: Array.isArray(v.evidence) ? (v.evidence as AskAnswer["evidence"]) : [] }
            : null,
      );
    } catch (err) {
      // Never an invented answer: say plainly that there is none.
      return { answer: err instanceof ApiError ? err.message : SERVER_MESSAGE, evidence: [] };
    }
  }
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
    { question },
  );
}

/**
 * Upload a receipt or invoice. Phase 0: accepted locally without a backend.
 *
 * On the static demo a photo or PDF is first read in this browser (lib/ocr.ts,
 * loaded only now) and what was read goes with the file; `onStage` reports it.
 */
export async function uploadEvidence(file: File, onStage?: (stage: "loading" | "reading" | "sending") => void): Promise<AnswerResult> {
  if (browserEngine) {
    const { readableKind, readInBrowser } = await import("./ocr");
    const reading = readableKind(file) ? await readInBrowser(file, onStage) : null;
    onStage?.("sending");
    return engineWrite("/api/evidence", {
      filename: file.name,
      contentType: file.type || null,
      dataBase64: await toBase64(file),
      ...(reading ? { reading } : {}),
    });
  }
  const body = new FormData();
  body.append("file", file);
  if (production) {
    const { ok, message } = await productionWrite("/api/evidence", { method: "POST", body }, 120000);
    return { ok, message };
  }
  if (!hasApi) {
    await pause(600 + Math.min(file.size / 2000, 900));
    return { ok: true };
  }
  return request<AnswerResult>("/api/evidence", { method: "POST", body }, () => ({ ok: true }), () => ({ ok: true }));
}

/* ---------- Diagram: what the engine is doing (engine or backend only; no sample) ---------- */

function isPipeline(v: unknown): Pipeline | null {
  return isRecord(v) && Array.isArray(v.stages) && Array.isArray(v.items) && isRecord(v.summary) ? (v as unknown as Pipeline) : null;
}

/** Null when there is no engine or backend to ask: the diagram shows real work only, never sample data. */
export async function getPipeline(): Promise<Pipeline | null> {
  if (!browserEngine && !hasApi) return null;
  return request<Pipeline | null>("/api/pipeline", { method: "GET" }, isPipeline, () => null);
}

/* ---------- Internal dashboard, "Admin OS" (engine or backend only; never sample data) ---------- */

function isOverview(v: unknown): InternalOverview | null {
  return isRecord(v) && Array.isArray(v.golden) && isRecord(v.health) && Array.isArray(v.fixes) && Array.isArray(v.tenants)
    ? (v as unknown as InternalOverview)
    : null;
}

function isOperations(v: unknown): InternalOperations | null {
  return isRecord(v) && Array.isArray(v.activity) && isRecord(v.audit) && Array.isArray(v.audit.entries)
    ? (v as unknown as InternalOperations)
    : null;
}

/** The team's Command Center figures. Null when there is no engine or backend to ask. */
export async function getInternalOverview(): Promise<InternalOverview | null> {
  if (!browserEngine && !hasApi) return null;
  return request<InternalOverview | null>("/api/internal/overview", { method: "GET" }, isOverview, () => null);
}

/** Recent activity and the newest `limit` audit records (the engine's default when omitted). */
export async function getInternalOperations(limit?: number): Promise<InternalOperations | null> {
  if (!browserEngine && !hasApi) return null;
  const path = `/api/internal/operations${limit ? `?limit=${encodeURIComponent(String(limit))}` : ""}`;
  return request<InternalOperations | null>(path, { method: "GET" }, isOperations, () => null);
}

function isAcceptance(v: unknown): InternalAcceptance | null {
  return isRecord(v) && Array.isArray(v.cases) && Array.isArray(v.sections) && isRecord(v.summary)
    ? (v as unknown as InternalAcceptance)
    : null;
}

/** The QA page: the 50 SME cases and the acceptance checklist with their verdicts. */
export async function getInternalAcceptance(): Promise<InternalAcceptance | null> {
  if (!browserEngine && !hasApi) return null;
  return request<InternalAcceptance | null>("/api/internal/acceptance", { method: "GET" }, isAcceptance, () => null);
}

/* ---------- Audit and accountant (sample data unless the browser engine runs) ---------- */

const isAudit: Guard<AuditResult> = (v) =>
  isRecord(v) && typeof v.companyName === "string" && Array.isArray(v.findings) ? (v as unknown as AuditResult) : null;

export async function getAudit(): Promise<AuditResult> {
  if (production) return productionRead("/api/audit", { method: "GET" }, isAudit);
  if (!browserEngine) return sample.audit;
  return viaEngine<AuditResult>(
    "GET",
    "/api/audit",
    undefined,
    isAudit,
    () => sample.audit,
  );
}

const isClients: Guard<AccountantClientRow[]> = (v) => {
  const list = arrayFrom(v, "clients");
  return list ? list.filter((c): c is AccountantClientRow => isRecord(c) && typeof c.id === "string") : null;
};

const isClient: Guard<AccountantClientDetail | null> = (v) =>
  isRecord(v) && typeof v.id === "string" && Array.isArray(v.evidence) ? (v as unknown as AccountantClientDetail) : null;

/** The accountant's clients: the engine's or the backend's (sample rows only when there is neither). */
export function getAccountantClients(): Promise<AccountantClientRow[]> {
  return request<AccountantClientRow[]>("/api/accountant/clients", { method: "GET" }, isClients, () => sample.accountantClients);
}

export function getAccountantClient(id: string): Promise<AccountantClientDetail | null> {
  const path = `/api/accountant/clients/${encodeURIComponent(id)}`;
  return request<AccountantClientDetail | null>(
    path,
    { method: "GET" },
    isClient,
    () => sample.accountantClientDetail(id),
    undefined,
    { value: null },
  );
}

/**
 * The accountant's firm, as the engine knows it (the business's accountant). Null when there is no
 * engine or backend to ask, or no accountant yet: the caller shows its own label.
 */
export async function getAccountantFirm(): Promise<{ name: string; person: string } | null> {
  if (!liveData) return null;
  const r = await call<{ default?: { firm?: string; name?: string } | null }>("GET", "/api/settings/accountant");
  const d = r.ok ? r.body.default : null;
  return d && typeof d.firm === "string" ? { name: d.firm, person: typeof d.name === "string" ? d.name : "" } : null;
}

/** Open one original from the accountant's view (an evidence link's `href`). Null, or a plain error message. */
export function openEvidence(href: string): Promise<string | null> {
  if (!liveData) return Promise.resolve("Connect the backend to open the originals.");
  return download(href);
}

/** Open one original by its evidence id (a bank line, a document, an email). Null, or a plain error message. */
export function openOriginal(evidenceId: string): Promise<string | null> {
  return openEvidence(`/api/evidence/${encodeURIComponent(evidenceId)}/file`);
}

/** Teach a rule for one client (`path` is the client's `links.rules`). `label` is the rule in plain words. */
export async function teachRule(
  path: string,
  text: string,
  scope: "client" | "all",
): Promise<AnswerResult & { label?: string }> {
  const r = await call<{ message?: string; rule?: { label?: string } }>("POST", path, { text, scope });
  const label = r.ok && typeof r.body.rule?.label === "string" ? r.body.rule.label : undefined;
  return { ok: r.ok, message: typeof r.body.message === "string" ? r.body.message : undefined, label };
}

/** Invite a client business: "Your accountant has enabled Back Office for you." (§29). */
export async function inviteClient(body: { email: string; clientName?: string; taxIds?: string[] }): Promise<AnswerResult> {
  const r = await call<{ ok?: boolean; message?: string }>("POST", "/api/accountant/invitations", body);
  // Written but not sent (no transport) is not a success: the reply says so with ok: false.
  return { ok: r.ok && r.body.ok !== false, message: typeof r.body.message === "string" ? r.body.message : undefined };
}

export async function getInvitations(): Promise<AccountantInvitation[]> {
  if (!liveData) return [];
  const r = await call<{ invitations?: unknown }>("GET", "/api/accountant/invitations");
  const list = r.ok ? arrayFrom(r.body, "invitations") : null;
  return list ? list.filter((i): i is AccountantInvitation => isRecord(i) && typeof i.id === "string") : [];
}

/** The invited owner accepts (production): the accountant can then see the business. */
export async function acceptInvitation(token: string): Promise<AnswerResult> {
  if (!production) return { ok: false, message: "Invitations are accepted in the live app." };
  const { ok, message } = await productionWrite("/api/invitations/accept", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token }),
  });
  return { ok, message };
}

export function getSources(): Promise<SourcesData> {
  return request<SourcesData>(
    "/api/sources",
    { method: "GET" },
    (v) => (isRecord(v) && Array.isArray(v.groups) ? (v as unknown as SourcesData) : null),
    () => sample.sources,
  );
}

export interface SourceChange {
  ok: boolean;
  message?: string;
  id?: string;
  authorizeUrl?: string;
}

async function sourceWrite(path: string, body: unknown): Promise<SourceChange> {
  if (browserEngine) {
    try {
      const reply = await engineRequest("POST", path, body);
      const b = isRecord(reply.body) ? reply.body : {};
      return {
        ok: reply.status === 200,
        message: typeof b.message === "string" ? b.message : undefined,
        id: typeof b.id === "string" ? b.id : undefined,
      };
    } catch (err) {
      warn(path, err);
      return { ok: false, message: "I couldn't save that. Try again." };
    }
  }
  if (!hasApi) return { ok: false, message: "Connect the backend to add sources." };
  try {
    const res = await apiFetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    if (production && res.status === 401) return toSignIn();
    const b: unknown = await res.json().catch(() => ({}));
    const r = isRecord(b) ? b : {};
    return {
      ok: res.ok,
      message: typeof r.message === "string" ? r.message : undefined,
      id: typeof r.id === "string" ? r.id : undefined,
      authorizeUrl: typeof r.authorizeUrl === "string" ? r.authorizeUrl : undefined,
    };
  } catch {
    return { ok: false, message: OFFLINE_MESSAGE };
  }
}

export function addSource(body: Record<string, unknown>): Promise<SourceChange> {
  return sourceWrite("/api/sources", body);
}

export function removeSource(id: string): Promise<SourceChange> {
  return sourceWrite(`/api/sources/${encodeURIComponent(id)}/remove`, {});
}

/* ---------- Chat operator, documents, report delivery, accountant API ---------- */

export interface CallResult<T = Record<string, unknown>> {
  ok: boolean;
  status: number;
  body: T;
}

/** Plain call to the engine (browser) or the backend (HTTP). No sample fallback: actions need a real engine. */
export async function call<T = Record<string, unknown>>(method: "GET" | "POST", path: string, body?: unknown): Promise<CallResult<T>> {
  const offline = { ok: false, status: 503, body: { message: "Connect the backend to use this." } as unknown as T };
  if (browserEngine) {
    try {
      const reply = await engineRequest(method, path, body);
      return { ok: reply.status === 200, status: reply.status, body: (reply.body ?? {}) as T };
    } catch {
      return { ...offline, body: { message: "I couldn't get ready. Reload the page." } as unknown as T };
    }
  }
  if (!hasApi) return offline;
  try {
    const res = await apiFetch(
      path,
      {
        method,
        headers: body !== undefined ? { "Content-Type": "application/json" } : {},
        body: method === "POST" ? JSON.stringify(body ?? {}) : undefined,
      },
      30000,
    );
    if (production && res.status === 401) return toSignIn();
    const parsed: unknown = await res.json().catch(() => ({}));
    return { ok: res.ok, status: res.status, body: parsed as T };
  } catch {
    return { ...offline, body: { message: OFFLINE_MESSAGE } as unknown as T };
  }
}

export function query(params: Record<string, string | undefined>): string {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v) q.set(k, v);
  const s = q.toString();
  return s ? `?${s}` : "";
}

/** Save a base64 file returned by the engine as a download. */
export function saveFile(file: { filename: string; contentType: string; data: string }) {
  const bin = atob(file.data);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  const url = URL.createObjectURL(new Blob([bytes], { type: file.contentType }));
  const a = document.createElement("a");
  a.href = url;
  a.download = file.filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function download(path: string, method: "GET" | "POST" = "GET", body?: unknown): Promise<string | null> {
  const r = await call<{ filename: string; contentType: string; data: string; message?: string }>(method, path, body);
  if (!r.ok || !r.body.data) return r.body.message ?? "I couldn't prepare that file.";
  saveFile(r.body);
  return null;
}
