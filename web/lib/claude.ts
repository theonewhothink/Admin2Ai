/**
 * The chat's Claude brain, running in the browser.
 *
 * The static site has no server to keep an API key, so the owner can add
 * their own Anthropic key. It is kept only in this browser (localStorage) and
 * sent only to api.anthropic.com. Without a key the chat uses the built-in
 * rules (POST /api/chat).
 *
 * The loop is the same as the server brain (backend/src/backoffice/assistant.py,
 * ClaudeBrain): the engine provides the instructions and tools
 * (GET /api/chat/tools) and runs every tool call (POST /api/chat/tool), so the
 * model only ever sees evidence and can only do what those tools allow: look
 * things up, draft emails the owner sends, record answers the owner gave, and
 * keep the owner's task list. It never moves money or approves bank details.
 */
import { call } from "./api";

const KEY_STORE = "admin2ai:anthropic-key";
const API = "https://api.anthropic.com/v1/messages";
const MAX_STEPS = 8;
const HISTORY_TURNS = 10;

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface BrainReply<Card> {
  reply: string;
  cards: Card[];
}

interface ToolSetup {
  system: string;
  tools: unknown[];
  today: string;
  model: string;
}

type Block =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> }
  | { type: string; [k: string]: unknown };

interface ApiResponse {
  content?: Block[];
  stop_reason?: string;
  error?: { type?: string; message?: string };
}

const listeners = new Set<() => void>();

export function claudeKey(): string | null {
  try {
    const k = window.localStorage.getItem(KEY_STORE);
    return k && k.trim() ? k.trim() : null;
  } catch {
    return null;
  }
}

/** Save (or with null, forget) the owner's key. Returns false if this browser would not keep it. */
export function setClaudeKey(key: string | null): boolean {
  try {
    if (key && key.trim()) window.localStorage.setItem(KEY_STORE, key.trim());
    else window.localStorage.removeItem(KEY_STORE);
    listeners.forEach((l) => l());
    return true;
  } catch {
    return false;
  }
}

export function subscribeClaudeKey(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (e: StorageEvent) => {
    if (e.key === KEY_STORE) listener();
  };
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}

/** A key looks like "sk-ant-…". Only a shape check; Anthropic decides. */
export function looksLikeKey(key: string): boolean {
  return /^sk-ant-[A-Za-z0-9_-]{20,}$/.test(key.trim());
}

let setup: Promise<ToolSetup | null> | null = null;

function loadSetup(): Promise<ToolSetup | null> {
  setup ??= call<ToolSetup>("GET", "/api/chat/tools").then((r) => (r.ok && Array.isArray(r.body.tools) ? r.body : null));
  void setup.then((s) => {
    if (!s) setup = null; // try again next time
  });
  return setup;
}

class ClaudeError extends Error {
  constructor(
    message: string,
    readonly keyProblem = false,
  ) {
    super(message);
  }
}

async function ask(key: string, body: Record<string, unknown>): Promise<ApiResponse> {
  let res: Response;
  try {
    res = await fetch(API, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
        "anthropic-dangerous-direct-browser-access": "true",
      },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(120_000),
    });
  } catch {
    throw new ClaudeError("I couldn't reach Claude. Check your connection and try again.");
  }
  const data = (await res.json().catch(() => ({}))) as ApiResponse;
  if (res.ok) return data;
  if (res.status === 401 || res.status === 403) {
    throw new ClaudeError("Anthropic did not accept your key. Check it in the chat settings.", true);
  }
  if (res.status === 429) throw new ClaudeError("Claude is busy or your key reached its limit. Try again in a minute.");
  if (res.status === 529 || res.status >= 500) throw new ClaudeError("Claude is overloaded right now. Try again in a minute.");
  throw new ClaudeError(data.error?.message ? `Claude could not answer: ${data.error.message}` : "Claude could not answer that.");
}

/**
 * One chat turn with Claude. Tool cards (documents, drafts, tasks, …) are
 * collected in the order the tools ran.
 */
export async function askClaude<Card>(key: string, message: string, history: ChatTurn[]): Promise<BrainReply<Card> & { keyProblem?: boolean }> {
  const cfg = await loadSetup();
  if (!cfg) return { reply: "I couldn't get ready. Reload the page and try again.", cards: [] };

  const messages: { role: string; content: unknown }[] = history
    .filter((h) => (h.role === "user" || h.role === "assistant") && h.content.trim())
    .slice(-HISTORY_TURNS)
    .map((h) => ({ role: h.role, content: h.content }));
  // The API needs the conversation to start with the owner.
  while (messages.length > 0 && messages[0]?.role !== "user") messages.shift();
  messages.push({ role: "user", content: `Today is ${cfg.today}. ${message}` });

  const cards: Card[] = [];
  try {
    for (let step = 0; step < MAX_STEPS; step++) {
      const res = await ask(key, {
        model: cfg.model,
        max_tokens: 4096,
        system: cfg.system,
        tools: cfg.tools,
        messages,
        output_config: { effort: "low" },
      });
      const content = res.content ?? [];
      if (res.stop_reason === "refusal") return { reply: "I can't help with that one.", cards };
      messages.push({ role: "assistant", content });
      if (res.stop_reason === "pause_turn") continue;
      const uses = content.filter((b): b is Extract<Block, { type: "tool_use" }> => b.type === "tool_use");
      if (res.stop_reason !== "tool_use" || uses.length === 0) {
        const text = content
          .filter((b): b is Extract<Block, { type: "text" }> => b.type === "text")
          .map((b) => b.text)
          .join("")
          .trim();
        return { reply: text || "Done.", cards };
      }
      const results = [];
      for (const use of uses) {
        const r = await call<{ isError?: boolean; result?: unknown; cards?: Card[]; message?: string }>("POST", "/api/chat/tool", {
          name: use.name,
          input: use.input ?? {},
        });
        if (r.ok) cards.push(...(r.body.cards ?? []));
        const isError = !r.ok || Boolean(r.body.isError);
        const out = r.ok ? r.body.result : (r.body.message ?? "That did not work.");
        results.push({
          type: "tool_result",
          tool_use_id: use.id,
          content: (typeof out === "string" ? out : JSON.stringify(out)).slice(0, 60_000),
          ...(isError ? { is_error: true } : {}),
        });
      }
      messages.push({ role: "user", content: results });
    }
    return { reply: "That took too many steps. Try asking in smaller parts.", cards };
  } catch (err) {
    if (err instanceof ClaudeError) return { reply: err.message, cards, keyProblem: err.keyProblem };
    return { reply: "Something went wrong. Try again.", cards };
  }
}
