import { describe, expect, it } from "@jest/globals";
import type { HttpRequest, HttpSend } from "../../api/http";
import { authCopy, httpAuthApi } from "../api";

function server(reply: { status: number; body: unknown } | "down") {
  const calls: HttpRequest[] = [];
  const send: HttpSend = async (req) => {
    calls.push(req);
    if (reply === "down") throw new Error("ECONNREFUSED");
    return { status: reply.status, header: () => null, text: async () => JSON.stringify(reply.body) };
  };
  return { calls, api: httpAuthApi({ baseUrl: "https://api.example.eu", timeoutMs: 1000, send }) };
}

const ok = { user: { id: "u1", email: "laura@example.pt", name: "Laura" }, tenant: { id: "t1", name: "Hazel Tree" }, token: "tok-1" };

describe("POST /api/auth/login", () => {
  it("returns the token and user, and sends the contract body without a CSRF header", async () => {
    const { calls, api } = server({ status: 200, body: ok });
    expect(await api.signIn(" laura@example.pt ", "correct horse battery")).toEqual({
      ok: true,
      token: "tok-1",
      user: { id: "u1", email: "laura@example.pt", name: "Laura" },
    });
    expect(calls[0]!.url).toBe("https://api.example.eu/api/auth/login");
    expect(JSON.parse(calls[0]!.body as string)).toEqual({ email: "laura@example.pt", password: "correct horse battery" });
    expect(calls[0]!.headers["X-Requested-With"]).toBeUndefined();
    expect(calls[0]!.headers.Authorization).toBeUndefined();
  });

  it("says plainly when the email or password is wrong", async () => {
    const { api } = server({ status: 401, body: { error: "unauthorized", message: "Email or password is not right." } });
    expect(await api.signIn("laura@example.pt", "nope")).toEqual({ ok: false, kind: "credentials", message: "Email or password is not right." });
    const bare = server({ status: 401, body: {} }).api;
    expect(await bare.signIn("laura@example.pt", "nope")).toMatchObject({ kind: "credentials", message: authCopy.credentials });
  });

  it("explains rate limiting, offline and server trouble without raw errors", async () => {
    expect(await server({ status: 429, body: {} }).api.signIn("a@b.pt", "x")).toMatchObject({ kind: "rateLimited", message: authCopy.rateLimited });
    expect(await server("down").api.signIn("a@b.pt", "x")).toMatchObject({ kind: "network", message: authCopy.network });
    expect(await server({ status: 500, body: { message: "Traceback (most recent call last)" } }).api.signIn("a@b.pt", "x")).toMatchObject({
      kind: "server",
      message: authCopy.server,
    });
    expect(await server({ status: 200, body: { user: ok.user } }).api.signIn("a@b.pt", "x")).toMatchObject({ kind: "server" });
  });
});

describe("POST /api/auth/logout", () => {
  it("sends the bearer token and never throws", async () => {
    const { calls, api } = server({ status: 204, body: "" });
    await api.signOut("tok-1");
    expect(calls[0]!.url).toBe("https://api.example.eu/api/auth/logout");
    expect(calls[0]!.headers.Authorization).toBe("Bearer tok-1");
    await expect(server("down").api.signOut("tok-1")).resolves.toBeUndefined();
  });
});
