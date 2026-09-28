/**
 * Owner API client for the phone.
 *
 *   GET  /api/home
 *   GET  /api/needs-you
 *   POST /api/needs-you/{id}/answer   { option_id, remember }
 *   GET  /api/activity
 *   POST /api/ask                     { question }
 *   POST /api/evidence/upload         (see ../offline/uploader.ts)
 *
 * Reads fall back to the last response seen on this phone, then to sample
 * data, and say which one they returned so the screen can label it. Writes
 * never pretend: an answer or a question that did not reach the server is
 * reported as not sent (§3). Only demo mode (no API configured) answers from
 * sample data.
 */
import { authHeaders, parseJson, type ApiEndpoint, type HttpSend } from "./http";
import { MemorySnapshotCache, type SnapshotCache } from "./cache";
import { parseActivity, parseAskAnswer, parseHome, parseNeedsYou } from "./guards";
import { sampleActivity, sampleAnswer, sampleHome, sampleNeedsYou } from "./sample";
import type { ActivityFeed, AskAnswer, HomeData, NeedsYouItem } from "./types";

export type DataSource = "live" | "cached" | "sample";

export interface Loaded<T> {
  data: T;
  source: DataSource;
  /** When the data was fetched from the server (live or cached); null for samples. */
  asOf: number | null;
  /** Why this is not live: no API configured, or the API could not be reached. */
  reason?: "demo" | "unreachable";
}

export type AskOutcome = { ok: true; answer: AskAnswer; source: "live" | "sample" } | { ok: false };

export interface ApiClientOptions {
  endpoint: ApiEndpoint;
  send: HttpSend;
  cache?: SnapshotCache;
  now?: () => number;
}

type Guard<T> = (value: unknown) => T | null;

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

  /** Record the owner's decision. `remember` asks the server to learn the rule (§34-41). */
  async answer(id: string, optionId: string, remember: boolean): Promise<{ ok: boolean }> {
    if (this.isDemo) return { ok: true };
    const res = await this.post(`/api/needs-you/${encodeURIComponent(id)}/answer`, { option_id: optionId, remember });
    return { ok: res !== null && res.status >= 200 && res.status < 300 };
  }

  async ask(question: string): Promise<AskOutcome> {
    const q = question.trim();
    if (!q) return { ok: false };
    if (this.isDemo) return { ok: true, answer: sampleAnswer(q), source: "sample" };
    const res = await this.post("/api/ask", { question: q });
    if (!res || res.status < 200 || res.status >= 300) return { ok: false };
    const answer = parseAskAnswer(parseJson(res.text));
    return answer ? { ok: true, answer, source: "live" } : { ok: false };
  }

  private async read<T>(path: string, key: string, guard: Guard<T>, sample: () => T): Promise<Loaded<T>> {
    const { baseUrl, timeoutMs } = this.options.endpoint;
    if (!baseUrl) return { data: sample(), source: "sample", asOf: null, reason: "demo" };
    try {
      const res = await this.options.send({
        method: "GET",
        url: `${baseUrl}${path}`,
        headers: { Accept: "application/json", ...(await authHeaders(this.options.endpoint)) },
        timeoutMs,
      });
      if (res.status >= 200 && res.status < 300) {
        const data = guard(parseJson(await res.text()));
        if (data !== null) {
          const asOf = this.now();
          await this.cache.set(key, data, asOf).catch(() => undefined);
          return { data, source: "live", asOf };
        }
      }
    } catch {
      // Unreachable: fall through to the cache.
    }
    const cached = await this.cache.get(key).catch(() => null);
    const data = cached ? guard(cached.data) : null;
    if (cached && data !== null) return { data, source: "cached", asOf: cached.savedAt, reason: "unreachable" };
    return { data: sample(), source: "sample", asOf: null, reason: "unreachable" };
  }

  private async post(path: string, body: unknown): Promise<{ status: number; text: string } | null> {
    const { baseUrl, timeoutMs } = this.options.endpoint;
    if (!baseUrl) return null;
    try {
      const res = await this.options.send({
        method: "POST",
        url: `${baseUrl}${path}`,
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          ...(await authHeaders(this.options.endpoint)),
        },
        body: JSON.stringify(body),
        timeoutMs,
      });
      return { status: res.status, text: await res.text() };
    } catch {
      return null;
    }
  }
}
