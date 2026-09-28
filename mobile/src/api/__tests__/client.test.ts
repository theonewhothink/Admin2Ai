import { describe, expect, it } from "@jest/globals";
import { ApiClient } from "../client";
import { MemorySnapshotCache, SealedSnapshotCache } from "../cache";
import { parseActivity, parseAskAnswer, parseHome, parseNeedsYou } from "../guards";
import { normalizeBaseUrl, type HttpRequest, type HttpSend } from "../http";
import { sampleActivity, sampleAnswer, sampleHome, sampleNeedsYou } from "../sample";
import type { RawFile } from "../../offline/journal";
import { NodeCipher } from "../../offline/__tests__/fakes";

type Route = { status: number; body: unknown } | "down";

function server(routes: Record<string, Route>) {
  const calls: HttpRequest[] = [];
  const send: HttpSend = async (req) => {
    calls.push(req);
    const route = routes[`${req.method} ${new URL(req.url).pathname}`];
    if (!route || route === "down") throw new Error("ECONNREFUSED");
    return { status: route.status, header: () => null, text: async () => JSON.stringify(route.body) };
  };
  return { calls, send };
}

const live = (send: HttpSend, cache = new MemorySnapshotCache()) =>
  new ApiClient({ endpoint: { baseUrl: "https://api.example.eu", timeoutMs: 1000, getAuthToken: async () => "t" }, send, cache, now: () => 1_000 });

describe("reads", () => {
  it("uses sample data in demo mode, labelled as demo", async () => {
    const client = new ApiClient({ endpoint: { baseUrl: null, timeoutMs: 1 }, send: server({}).send });
    expect(await client.getHome()).toEqual({ data: sampleHome, source: "sample", asOf: null, reason: "demo" });
  });

  it("returns live data, sends the token, and caches it", async () => {
    const { calls, send } = server({ "GET /api/home": { status: 200, body: { ...sampleHome, currentMonth: { key: "2026-09", label: "September", percentClosed: 94 } } } });
    const cache = new MemorySnapshotCache();
    const res = await live(send, cache).getHome();
    expect(res.source).toBe("live");
    expect(res.asOf).toBe(1_000);
    expect(calls[0]!.headers.Authorization).toBe("Bearer t");
    expect((await cache.get("home"))?.savedAt).toBe(1_000);
  });

  it("falls back to the last known data, then to labelled samples", async () => {
    const cache = new MemorySnapshotCache();
    await live(server({ "GET /api/activity": { status: 200, body: sampleActivity } }).send, cache).getActivity();
    const offline = live(server({ "GET /api/activity": "down" }).send, cache);
    expect(await offline.getActivity()).toMatchObject({ source: "cached", asOf: 1_000, reason: "unreachable" });

    const nothing = live(server({ "GET /api/needs-you": "down" }).send);
    expect(await nothing.getNeedsYou()).toMatchObject({ source: "sample", reason: "unreachable", data: sampleNeedsYou });
  });

  it("treats error statuses and malformed bodies as unreachable", async () => {
    const res = await live(server({ "GET /api/home": { status: 500, body: {} } }).send).getHome();
    expect(res.source).toBe("sample");
    const bad = await live(server({ "GET /api/home": { status: 200, body: { greeting: "hi" } } }).send).getHome();
    expect(bad.source).toBe("sample");
  });
});

describe("writes never pretend", () => {
  it("posts answers with option_id and remember", async () => {
    const { calls, send } = server({ "POST /api/needs-you/nd_ikea_418/answer": { status: 200, body: { ok: true } } });
    expect(await live(send).answer("nd_ikea_418", "hazel-tree", true)).toEqual({ ok: true });
    expect(JSON.parse(calls[0]!.body as string)).toEqual({ option_id: "hazel-tree", remember: true });
  });

  it("reports an answer as not sent when the server is down or refuses", async () => {
    expect(await live(server({}).send).answer("x", "y", false)).toEqual({ ok: false });
    expect(await live(server({ "POST /api/needs-you/x/answer": { status: 409, body: {} } }).send).answer("x", "y", false)).toEqual({ ok: false });
  });

  it("answers questions live, and never from samples when live fails", async () => {
    const ok = server({ "POST /api/ask": { status: 200, body: { answer: "Yes.", evidence: [{ label: "Invoice", id: "doc:1" }, { label: "" }] } } });
    expect(await live(ok.send).ask("Did we pay Vodafone?")).toEqual({
      ok: true,
      source: "live",
      answer: { answer: "Yes.", evidence: [{ label: "Invoice", id: "doc:1" }] },
    });
    expect(await live(server({}).send).ask("Did we pay Vodafone?")).toEqual({ ok: false });
    expect(await live(ok.send).ask("   ")).toEqual({ ok: false });
  });

  it("uses sample answers only in demo mode", async () => {
    const demo = new ApiClient({ endpoint: { baseUrl: null, timeoutMs: 1 }, send: server({}).send });
    expect(await demo.ask("did we pay vodafone")).toMatchObject({ ok: true, source: "sample" });
    expect(sampleAnswer("What's the weather?").evidence).toEqual([]);
  });
});

describe("guards", () => {
  it("normalises money to decimal strings and drops malformed items", () => {
    const items = parseNeedsYou({
      items: [
        { ...sampleNeedsYou[0], amount: 418 },
        { ...sampleNeedsYou[1], verification: { optionLabel: "x" } },
        { id: "z", kind: "mystery" },
      ],
    });
    expect(items).toHaveLength(1);
    expect(items![0]!.amount).toBe("418");
  });

  it("treats unknown connection status as stale, never healthy", () => {
    const home = parseHome({ ...sampleHome, connections: [{ ...sampleHome.connections[0], status: "weird" }] });
    expect(home?.connections[0]?.status).toBe("stale");
  });

  it("rejects impossible values", () => {
    expect(parseHome({ ...sampleHome, currentMonth: { key: "2026-09", label: "September", percentClosed: 140 } })).toBeNull();
    expect(parseHome({ ...sampleHome, needsYouCount: -1 })).toBeNull();
    expect(parseActivity({ items: [{ id: "a", at: "x", kind: "hacked", text: "t" }] })?.items).toEqual([]);
    expect(parseAskAnswer({ answer: "" })).toBeNull();
  });

  it("normalises the configured base URL", () => {
    expect(normalizeBaseUrl(" https://api.example.eu/// ")).toBe("https://api.example.eu");
    expect(normalizeBaseUrl("ftp://x")).toBeNull();
    expect(normalizeBaseUrl(undefined)).toBeNull();
  });
});

describe("sealed snapshot cache", () => {
  it("stores screens encrypted and survives a restart", async () => {
    let stored: Uint8Array | null = null;
    const file: RawFile = { read: async () => stored, writeAtomic: async (b) => void (stored = b) };
    const cipher = new NodeCipher();
    await new SealedSnapshotCache(file, cipher).set("home", sampleHome, 5);
    expect(Buffer.from(stored!).includes(Buffer.from("Hazel Tree"))).toBe(false);
    expect(await new SealedSnapshotCache(file, cipher).get("home")).toEqual({ savedAt: 5, data: sampleHome });
    expect(await new SealedSnapshotCache(file, new NodeCipher(new Uint8Array(32).fill(3))).get("home")).toBeNull();
  });
});
