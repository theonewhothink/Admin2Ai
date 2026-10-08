/**
 * Owner API client for the phone. Every call sends `Authorization: Bearer
 * <token>` when the owner is signed in (bearer requests need no CSRF header).
 *
 *   GET  /api/home
 *   GET  /api/needs-you
 *   POST /api/needs-you/{id}/answer   { option_id, remember }
 *   GET  /api/activity
 *   POST /api/ask                     { question }
 *   GET  /api/sources                 → what I read, how every payment stands, the companies
 *   POST /api/sources/understand      { text } → { kind, fields, message } (only reads)
 *   GET  /api/auth/me                 → { user, tenant, role }
 *   POST /api/devices                 { expoPushToken, platform } → 204
 *   POST /api/devices/remove          { expoPushToken } → 204
 *   POST /api/evidence/upload         (see ../offline/uploader.ts)
 *
 * Reads fall back to the last response seen on this phone, then to sample
 * data, and say which one they returned so the screen can label it. Writes
 * never pretend: an answer or a question that did not reach the server is
 * reported as not sent (§3). Only demo mode (no API configured) answers from
 * sample data.
 *
 * A 401 means the session is missing or has ended: the client reports it
 * (endpoint.onUnauthorized, which sends the owner to sign in) and never shows
 * cached data for it, since that data may belong to the session that ended.
 */
import { authHeaders, isRecord, parseJson, type ApiEndpoint, type HttpSend } from "./http";
import { MemorySnapshotCache, type SnapshotCache } from "./cache";
import { parseActivity, parseAskAnswer, parseHome, parseNeedsYou, parseSources, parseUnderstood } from "./guards";
import { sampleActivity, sampleAnswer, sampleHome, sampleNeedsYou, sampleSources } from "./sample";
import type { ActivityFeed, AskAnswer, HomeData, NeedsYouItem, SourcesData, UnderstoodSource } from "./types";

export type DataSource = "live" | "cached" | "sample";

export interface Loaded<T> {
  data: T;
  source: DataSource;
  /** When the data was fetched from the server (live or cached); null for samples. */
  asOf: number | null;
  /** Why this is not live: no API configured, the API could not be reached, or the session ended. */
  reason?: "demo" | "unreachable" | "signedOut";
}

export type AskOutcome = { ok: true; answer: AskAnswer; source: "live" | "sample" } | { ok: false };

/** "Something missing?": understood by the server, or why not ("demo": nothing to add to in the demo). */
export type UnderstandOutcome = { ok: true; understood: UnderstoodSource } | { ok: false; reason: "demo" | "unreachable" };

export interface Me {
  user: { id: string; email: string; name: string };
  tenant: { id: string; name: string };
  role: string;
}

export interface ApiClientOptions {
  endpoint: ApiEndpoint;
  send: HttpSend;
  cache?: SnapshotCache;
  now?: () => number;
}

type Guard<T> = (value: unknown) => T | null;

export function parseMe(value: unknown): Me | null {
  if (!isRecord(value) || !isRecord(value.user) || typeof value.user.email !== "string") return null;
  const u = value.user;
  const t = isRecord(value.tenant) ? value.tenant : {};
  return {
    user: { id: String(u.id ?? ""), email: u.email as string, name: typeof u.name === "string" ? u.name : "" },
    tenant: { id: String(t.id ?? ""), name: typeof t.name === "string" ? t.name : "" },
    role: typeof value.role === "string" ? value.role : "owner",
  };
}

export class ApiClient {
  private readonly cache: SnapshotCache;
  private readonly now: () => number;

  constructor(private readonly options: ApiClientOptions) {
    this.cache = options.cache ?? new MemorySnapshotCache();
    this.now = options.now ?? Date.now;
  }

  get isDemo(): boolean {
    return this.options.endpoint.baseUrl === null;
  }

  getHome(): Promise<Loaded<HomeData>> {
    return this.read("/api/home", "home", parseHome, () => sampleHome);
  }

  getNeedsYou(): Promise<Loaded<NeedsYouItem[]>> {
    return this.read("/api/needs-you", "needs-you", parseNeedsYou, () => sampleNeedsYou);
  }

  getActivity(): Promise<Loaded<ActivityFeed>> {
    return this.read("/api/activity", "activity", parseActivity, () => sampleActivity);
  }

  getSources(): Promise<Loaded<SourcesData>> {
    return this.read("/api/sources", "sources", parseSources, () => sampleSources);
  }

  /** What the owner typed in "Something missing?", understood. It only reads: nothing is added. */
  async understandSource(text: string): Promise<UnderstandOutcome> {
    const t = text.trim();
    if (this.isDemo) return { ok: false, reason: "demo" };
    if (!t) return { ok: false, reason: "unreachable" };
    const res = await this.request("POST", "/api/sources/understand", { text: t });
    const understood = res && res.status >= 200 && res.status < 300 ? parseUnderstood(parseJson(res.text)) : null;
    return understood ? { ok: true, understood } : { ok: false, reason: "unreachable" };
  }

  /** The signed-in owner, or null (demo mode, offline, or signed out). */
  async getMe(): Promise<Me | null> {
    const res = await this.request("GET", "/api/auth/me");
    return res && res.status === 200 ? parseMe(parseJson(res.text)) : null;
  }

  /** Record the owner's decision. `remember` asks the server to learn the rule (§34-41). */
  async answer(id: string, optionId: string, remember: boolean): Promise<{ ok: boolean }> {
    if (this.isDemo) return { ok: true };
    const res = await this.request("POST", `/api/needs-you/${encodeURIComponent(id)}/answer`, { option_id: optionId, remember });
    return { ok: res !== null && res.status >= 200 && res.status < 300 };
  }

  async ask(question: string): Promise<AskOutcome> {
    const q = question.trim();
    if (!q) return { ok: false };
    if (this.isDemo) return { ok: true, answer: sampleAnswer(q), source: "sample" };
    const res = await this.request("POST", "/api/ask", { question: q });
    if (!res || res.status < 200 || res.status >= 300) return { ok: false };
    const answer = parseAskAnswer(parseJson(res.text));
    return answer ? { ok: true, answer, source: "live" } : { ok: false };
  }

  /** Tell the server where to send this phone's rare notifications (§42). */
  async registerDevice(expoPushToken: string, platform: "ios" | "android"): Promise<boolean> {
    const res = await this.request("POST", "/api/devices", { expoPushToken, platform });
    return res !== null && res.status >= 200 && res.status < 300;
  }

  /** Stop notifications to this phone (sign-out). */
  async removeDevice(expoPushToken: string): Promise<boolean> {
    const res = await this.request("POST", "/api/devices/remove", { expoPushToken });
    return res !== null && res.status >= 200 && res.status < 300;
  }

  /** Forget the screens this phone remembered (sign-out: they belong to that session). */
  forgetCachedScreens(): Promise<void> {
    return this.cache.clear().catch(() => undefined);
  }

  private async read<T>(path: string, key: string, guard: Guard<T>, sample: () => T): Promise<Loaded<T>> {
    const { baseUrl } = this.options.endpoint;
    if (!baseUrl) return { data: sample(), source: "sample", asOf: null, reason: "demo" };
    const res = await this.request("GET", path);
    if (res?.status === 401) return { data: sample(), source: "sample", asOf: null, reason: "signedOut" };
    if (res && res.status >= 200 && res.status < 300) {
      const data = guard(parseJson(res.text));
      if (data !== null) {
        const asOf = this.now();
        await this.cache.set(key, data, asOf).catch(() => undefined);
        return { data, source: "live", asOf };
      }
    }
    const cached = await this.cache.get(key).catch(() => null);
    const data = cached ? guard(cached.data) : null;
    if (cached && data !== null) return { data, source: "cached", asOf: cached.savedAt, reason: "unreachable" };
    return { data: sample(), source: "sample", asOf: null, reason: "unreachable" };
  }

  /** One call. Null when there is no server or it could not be reached. A 401 is reported, then returned. */
  private async request(method: "GET" | "POST", path: string, body?: unknown): Promise<{ status: number; text: string } | null> {
    const { baseUrl, timeoutMs } = this.options.endpoint;
    if (!baseUrl) return null;
    let res: { status: number; text: string };
    try {
      const response = await this.options.send({
        method,
        url: `${baseUrl}${path}`,
        headers: {
          Accept: "application/json",
          ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
          ...(await authHeaders(this.options.endpoint)),
        },
        ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
        timeoutMs,
      });
      res = { status: response.status, text: await response.text() };
    } catch {
      return null;
    }
    if (res.status === 401) this.options.endpoint.onUnauthorized?.();
    return res;
  }
}
