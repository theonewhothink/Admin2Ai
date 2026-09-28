/**
 * The in-browser engine (NEXT_PUBLIC_ENGINE=browser).
 *
 * The real Python back-office engine runs in a Web Worker on Pyodide
 * (public/engine/worker.js). Requests that would go to the HTTP API are sent
 * to BackOfficeService.dispatch() in the worker instead, so the static site
 * works with no server at all.
 *
 * Every change the visitor makes (answers, uploads, …) is kept in
 * sessionStorage and replayed when the engine starts again, so a reload keeps
 * their progress for the rest of the visit.
 */

export const browserEngine = process.env.NEXT_PUBLIC_ENGINE === "browser";

const BASE_PATH = (process.env.NEXT_PUBLIC_BASE_PATH ?? "").replace(/\/+$/, "");
const VERSION = process.env.NEXT_PUBLIC_ENGINE_BUILD ?? "dev";
const JOURNAL_KEY = "admin2ai:engine-journal";
const JOURNAL_MAX_CHARS = 3_000_000;
/** Requests that change the engine's state and so must be replayed after a reload. */
const MUTATING = /^\/api\/(sources(\/[^/]+\/remove)?|needs-you\/[^/]+\/answer|evidence(\/upload)?|receipts|share|connections\/[^/]+\/(stale|reconnect)|accountant\/rules)$/;

export interface EngineReply {
  status: number;
  body: unknown;
}

export interface EngineInfo {
  python: string;
  pyodide: string;
  ms: number;
  replayed: number;
}

type EngineState = { phase: "idle" | "starting" } | { phase: "ready"; info: EngineInfo } | { phase: "failed"; message: string };

interface JournalEntry {
  method: string;
  path: string;
  body: unknown;
}

let worker: Worker | null = null;
let ready: Promise<EngineInfo> | null = null;
let nextId = 1;
let state: EngineState = { phase: "idle" };
const pending = new Map<number, (reply: EngineReply) => void>();
const listeners = new Set<() => void>();

function setState(next: EngineState) {
  state = next;
  listeners.forEach((l) => l());
}

/** Current engine state, for the loading screen. */
export function engineState(): EngineState {
  return state;
}

export function subscribeEngine(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function readJournal(): JournalEntry[] {
  try {
    const raw = window.sessionStorage.getItem(JOURNAL_KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as JournalEntry[]) : [];
  } catch {
    return [];
  }
}

function remember(entry: JournalEntry) {
  try {
    const next = JSON.stringify([...readJournal(), entry]);
    if (next.length <= JOURNAL_MAX_CHARS) window.sessionStorage.setItem(JOURNAL_KEY, next);
  } catch {
    // Storage full or unavailable: the change still applies until the tab reloads.
  }
}

/** Forget everything the visitor changed and start again from the demo. */
export function resetEngine() {
  try {
    window.sessionStorage.removeItem(JOURNAL_KEY);
  } catch {
    // ignore
  }
  worker?.terminate();
  worker = null;
  ready = null;
  pending.clear();
  setState({ phase: "idle" });
}

/** Start the engine (once) and resolve when it can answer requests. */
export function startEngine(): Promise<EngineInfo> {
  if (ready) return ready;
  setState({ phase: "starting" });
  ready = new Promise<EngineInfo>((resolve, reject) => {
    const w = new Worker(`${BASE_PATH}/engine/worker.js?v=${encodeURIComponent(VERSION)}`, { type: "module" });
    worker = w;
    w.onmessage = (event: MessageEvent) => {
      const msg = event.data as { type: string; id?: number; status?: number; body?: unknown; message?: string } & Partial<EngineInfo>;
      if (msg.type === "reply" && typeof msg.id === "number") {
        const done = pending.get(msg.id);
        pending.delete(msg.id);
        done?.({ status: msg.status ?? 500, body: msg.body });
      } else if (msg.type === "ready") {
        const info: EngineInfo = {
          python: msg.python ?? "",
          pyodide: msg.pyodide ?? "",
          ms: msg.ms ?? 0,
          replayed: msg.replayed ?? 0,
        };
        document.documentElement.dataset.engine = "python";
        console.info(
          `[engine] Python ${info.python} (Pyodide ${info.pyodide}) ready in ${info.ms} ms` +
            (info.replayed ? `, replayed ${info.replayed} earlier changes` : ""),
        );
        setState({ phase: "ready", info });
        resolve(info);
      } else if (msg.type === "failed") {
        document.documentElement.dataset.engine = "failed";
        console.warn(`[engine] could not start: ${msg.message}`);
        setState({ phase: "failed", message: msg.message ?? "" });
        reject(new Error(msg.message));
      }
    };
    w.onerror = (event) => {
      event.preventDefault();
      const message = event.message || "the engine could not load";
      console.warn(`[engine] could not start: ${message}`);
      setState({ phase: "failed", message });
      reject(new Error(message));
    };
    w.postMessage({ type: "init", version: VERSION, journal: readJournal() });
  });
  // Callers handle the rejection; this keeps an unobserved failure from being reported twice.
  ready.catch(() => undefined);
  return ready;
}

/** Send one request to the engine. Resolves with the engine's status and JSON body. */
export async function engineRequest(method: string, path: string, body?: unknown): Promise<EngineReply> {
  await startEngine();
  const w = worker;
  if (!w) throw new Error("engine stopped");
  const id = nextId++;
  const reply = await new Promise<EngineReply>((resolve) => {
    pending.set(id, resolve);
    w.postMessage({ type: "call", id, method, path, body: body ?? null });
  });
  if (reply.status === 200 && method !== "GET" && MUTATING.test(path)) {
    remember({ method, path, body: body ?? null });
  }
  return reply;
}
