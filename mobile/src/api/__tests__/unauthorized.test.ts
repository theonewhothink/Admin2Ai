import { describe, expect, it, jest } from "@jest/globals";
import { emitUnauthorized, onUnauthorized } from "../../auth/events";
import { HttpEvidenceUploader } from "../../offline/uploader";
import type { UploadRequest } from "../../offline/types";
import { MemorySnapshotCache } from "../cache";
import { ApiClient } from "../client";
import type { ApiEndpoint, HttpRequest, HttpSend } from "../http";
import { sampleHome, sampleNeedsYou } from "../sample";

type Route = { status: number; body: unknown };

function server(routes: Record<string, Route>) {
  const calls: HttpRequest[] = [];
  const send: HttpSend = async (req) => {
    calls.push(req);
    const route = routes[`${req.method} ${new URL(req.url).pathname}`];
    if (!route) throw new Error("ECONNREFUSED");
    return { status: route.status, header: () => null, text: async () => JSON.stringify(route.body) };
  };
  return { calls, send };
}

function endpoint(token: string | null = "tok"): ApiEndpoint & { onUnauthorized: jest.Mock<() => void> } {
  return { baseUrl: "https://api.example.eu", timeoutMs: 1000, getAuthToken: async () => token, onUnauthorized: jest.fn() };
}

describe("a 401 on the phone", () => {
  it("reports the ended session and never shows another session's cached screen", async () => {
    const cache = new MemorySnapshotCache();
    await cache.set("home", sampleHome, 5);
    const ep = endpoint();
    const client = new ApiClient({ endpoint: ep, send: server({ "GET /api/home": { status: 401, body: { error: "unauthorized" } } }).send, cache });
    const res = await client.getHome();
    expect(ep.onUnauthorized).toHaveBeenCalledTimes(1);
    expect(res).toMatchObject({ source: "sample", reason: "signedOut", asOf: null });
  });

  it("reports it for writes too, and says the write did not happen", async () => {
    const ep = endpoint();
    const client = new ApiClient({ endpoint: ep, send: server({ "POST /api/needs-you/x/answer": { status: 401, body: {} } }).send });
    expect(await client.answer("x", "y", false)).toEqual({ ok: false });
    expect(ep.onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("does not treat other refusals (403, 500) as signed out", async () => {
    const ep = endpoint();
    const client = new ApiClient({ endpoint: ep, send: server({ "GET /api/needs-you": { status: 403, body: {} } }).send });
    expect((await client.getNeedsYou()).reason).toBe("unreachable");
    expect(ep.onUnauthorized).not.toHaveBeenCalled();
  });

  it("sends every call with the bearer token", async () => {
    const { calls, send } = server({ "GET /api/needs-you": { status: 200, body: { items: sampleNeedsYou } } });
    await new ApiClient({ endpoint: endpoint("tok-9"), send }).getNeedsYou();
    expect(calls[0]!.headers.Authorization).toBe("Bearer tok-9");
  });

  it("reaches every listener in the runtime (screens and the upload queue share one signal)", () => {
    const a = jest.fn();
    const b = jest.fn();
    const stopA = onUnauthorized(a);
    const stopB = onUnauthorized(() => {
      b();
      throw new Error("a broken listener must not stop the others");
    });
    emitUnauthorized();
    stopA();
    stopB();
    emitUnauthorized();
    expect(a).toHaveBeenCalledTimes(1);
    expect(b).toHaveBeenCalledTimes(1);
  });
});

describe("account and device calls", () => {
  it("reads the signed-in owner", async () => {
    const me = { user: { id: "u1", email: "laura@example.pt", name: "Laura" }, tenant: { id: "t1", name: "Hazel Tree" }, role: "owner" };
    const client = new ApiClient({ endpoint: endpoint(), send: server({ "GET /api/auth/me": { status: 200, body: me } }).send });
    expect(await client.getMe()).toEqual(me);
    expect(await new ApiClient({ endpoint: { baseUrl: null, timeoutMs: 1 }, send: server({}).send }).getMe()).toBeNull();
  });

  it("registers and removes this phone for notifications with the contract bodies", async () => {
    const { calls, send } = server({ "POST /api/devices": { status: 204, body: "" }, "POST /api/devices/remove": { status: 204, body: "" } });
    const client = new ApiClient({ endpoint: endpoint(), send });
    expect(await client.registerDevice("ExponentPushToken[abc]", "ios")).toBe(true);
    expect(await client.removeDevice("ExponentPushToken[abc]")).toBe(true);
    expect(JSON.parse(calls[0]!.body as string)).toEqual({ expoPushToken: "ExponentPushToken[abc]", platform: "ios" });
    expect(JSON.parse(calls[1]!.body as string)).toEqual({ expoPushToken: "ExponentPushToken[abc]" });
    expect(calls[0]!.headers.Authorization).toBe("Bearer tok");
    expect(await new ApiClient({ endpoint: endpoint(), send: server({}).send }).registerDevice("t", "android")).toBe(false);
  });

  it("forgets cached screens on sign-out", async () => {
    const cache = new MemorySnapshotCache();
    await cache.set("home", sampleHome, 5);
    await new ApiClient({ endpoint: endpoint(), send: server({}).send, cache }).forgetCachedScreens();
    expect(await cache.get("home")).toBeNull();
  });
});

describe("uploads and the session", () => {
  const request: UploadRequest = {
    idempotencyKey: "item-1",
    sha256: "a".repeat(64),
    bytes: new Uint8Array([0xff, 0xd8, 1, 2, 3, 0xff, 0xd9]),
    meta: { source: "mobile_scan", format: "image", capturedAt: "2026-10-02T09:30:00+01:00", fileName: "receipt.jpg", mimeType: "image/jpeg" },
  };

  it("never sends evidence without a session: it stays queued", async () => {
    const { calls, send } = server({});
    const uploader = new HttpEvidenceUploader({ endpoint: endpoint(null), send, random: () => 0.5, now: () => 0 });
    expect(await uploader.upload(request)).toEqual({ kind: "auth", status: 401 });
    expect(calls).toHaveLength(0);
  });

  it("reports a 401 from the upload endpoint", async () => {
    const ep = endpoint();
    const { send } = server({ "POST /api/evidence/upload": { status: 401, body: {} } });
    const uploader = new HttpEvidenceUploader({ endpoint: ep, send, random: () => 0.5, now: () => 0 });
    expect(await uploader.upload(request)).toEqual({ kind: "auth", status: 401 });
    expect(ep.onUnauthorized).toHaveBeenCalledTimes(1);
  });
});
