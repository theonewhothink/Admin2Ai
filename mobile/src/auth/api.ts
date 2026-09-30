/**
 * Sign-in over HTTP (production API contract). Pure: the transport is the
 * shared HttpSend port, so tests pass fakes.
 *
 *   POST /api/auth/login  {email, password} → 200 {user, tenant, token}
 *                         401 {error, message} wrong email or password
 *                         429 too many attempts (10 per 15 minutes)
 *   POST /api/auth/logout (Bearer) → 204
 *
 * Bearer requests need no CSRF header (that guard is for cookie sessions).
 */
import { isRecord, parseJson, type HttpSend } from "../api/http";
import type { AuthApi, AuthUser, SignInOutcome } from "./store";

export const authCopy = {
  credentials: "Email or password is not right.",
  rateLimited: "Too many attempts. Wait 15 minutes, then try again.",
  network: "I can't reach Back Office. Check your connection and try again.",
  server: "Something went wrong on our side. Try again in a moment.",
} as const;

function serverMessage(body: unknown): string | null {
  if (!isRecord(body) || typeof body.message !== "string") return null;
  const m = body.message.trim();
  return m && m.length <= 300 && !/traceback|exception/i.test(m) ? m : null;
}

export function parseSignIn(body: unknown): { token: string; user: AuthUser } | null {
  if (!isRecord(body) || typeof body.token !== "string" || !body.token) return null;
  const u = isRecord(body.user) ? body.user : {};
  if (typeof u.email !== "string") return null;
  return {
    token: body.token,
    user: { id: String(u.id ?? ""), email: u.email, name: typeof u.name === "string" ? u.name : "" },
  };
}

export function httpAuthApi(options: { baseUrl: string; timeoutMs: number; send: HttpSend }): AuthApi {
  const { baseUrl, timeoutMs, send } = options;
  return {
    async signIn(email, password): Promise<SignInOutcome> {
      let status: number;
      let text: string;
      try {
        const res = await send({
          method: "POST",
          url: `${baseUrl}/api/auth/login`,
          headers: { Accept: "application/json", "Content-Type": "application/json" },
          body: JSON.stringify({ email: email.trim(), password }),
          timeoutMs,
        });
        status = res.status;
        text = await res.text();
      } catch {
        return { ok: false, kind: "network", message: authCopy.network };
      }
      const body = parseJson(text);
      if (status === 200) {
        const parsed = parseSignIn(body);
        return parsed ? { ok: true, ...parsed } : { ok: false, kind: "server", message: authCopy.server };
      }
      if (status === 401) return { ok: false, kind: "credentials", message: serverMessage(body) ?? authCopy.credentials };
      if (status === 429) return { ok: false, kind: "rateLimited", message: serverMessage(body) ?? authCopy.rateLimited };
      return { ok: false, kind: "server", message: (status < 500 && serverMessage(body)) || authCopy.server };
    },

    async signOut(token) {
      try {
        await send({
          method: "POST",
          url: `${baseUrl}/api/auth/logout`,
          headers: { Accept: "application/json", "Content-Type": "application/json", Authorization: `Bearer ${token}` },
          body: "{}",
          timeoutMs,
        });
      } catch {
        // Offline: the token is deleted from the phone anyway, and it expires on the server.
      }
    },
  };
}
