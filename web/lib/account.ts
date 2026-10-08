/**
 * Sign-in, sign-up, the signed-in owner, onboarding connections and the
 * account (export, delete). Production only (see ./mode.ts): the demo and the
 * sample site have no accounts.
 *
 * Endpoints (the production API contract):
 *   POST /api/auth/signup  {email, password, name, companyName, taxId?} → 201 {user, tenant, token}
 *   POST /api/auth/login   {email, password} → 200 {user, tenant, token} | 401 | 429
 *   POST /api/auth/logout  → 204
 *   GET  /api/auth/me      → 200 {user, tenant, role} | 401
 *   GET  /api/account/export → application/zip
 *   POST /api/account/delete {confirm: "DELETE", password} → 202
 *   POST /api/onboarding/company {name, taxId, legalName?}
 *   POST /api/onboarding/accountant {email, name?}
 *   GET  /api/oauth/start?provider=google|microsoft → 302 to the provider
 *   POST /api/connections/bank/start {institutionId} → {redirectUrl}
 *
 * The web app never reads the token in the JSON replies: the API sets the
 * HttpOnly session cookie, and every call sends it (credentials: "include").
 */
import { API_URL, BASE_PATH } from "./mode";
import { OFFLINE_MESSAGE, apiFetch, downloadFile, errorMessage, toSignIn } from "./api";
import type { Session } from "./owner";

export type { Session, SessionUser } from "./owner";

export type Result<T> = { ok: true; value: T } | { ok: false; status: number; message: string; field?: string };

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** {user, tenant, role?} → Session, or null when the shape is wrong. */
export function parseSession(v: unknown): Session | null {
  if (!isRecord(v) || !isRecord(v.user)) return null;
  const u = v.user;
  if (typeof u.email !== "string") return null;
  const t = isRecord(v.tenant) ? v.tenant : {};
  return {
    user: { id: String(u.id ?? ""), email: u.email, name: typeof u.name === "string" ? u.name : "" },
    tenant: { id: String(t.id ?? ""), name: typeof t.name === "string" ? t.name : "" },
    role: typeof v.role === "string" ? v.role : "owner",
  };
}

/**
 * Where to go after signing in. Only a path inside this app: anything else
 * (another site, "//evil.example", the sign-in pages themselves) becomes Home.
 */
export function safeNext(raw: string | null | undefined): string {
  if (!raw || !raw.startsWith("/") || raw.startsWith("//") || raw.startsWith("/\\")) return "/";
  if (/[\u0000-\u001f]/.test(raw)) return "/";
  let url: URL;
  try {
    url = new URL(raw, "https://admin2ai.invalid");
  } catch {
    return "/";
  }
  if (url.origin !== "https://admin2ai.invalid") return "/";
  if (/^\/sign(in|up)(\/|$)/.test(url.pathname)) return "/";
  return `${url.pathname}${url.search}${url.hash}`;
}

async function readJson(res: Response): Promise<unknown> {
  try {
    const text = await res.text();
    return text ? (JSON.parse(text) as unknown) : {};
  } catch {
    return {};
  }
}

async function post(path: string, body: unknown, timeoutMs?: number): Promise<Response | null> {
  try {
    return await apiFetch(
      path,
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      timeoutMs,
    );
  } catch {
    return null;
  }
}

const offline = <T>(): Result<T> => ({ ok: false, status: 0, message: OFFLINE_MESSAGE });

/* ---------- Session ---------- */

/** The signed-in owner, or null when nobody is signed in. Never redirects. */
export async function getSession(): Promise<Session | null> {
  let res: Response;
  try {
    res = await apiFetch("/api/auth/me");
  } catch {
    return null;
  }
  if (!res.ok) return null;
  return parseSession(await readJson(res));
}

/** The signed-in owner. Without a session, goes to sign-in and never resolves. */
export async function requireSession(): Promise<Session> {
  let res: Response;
  try {
    res = await apiFetch("/api/auth/me");
  } catch {
    throw new Error(OFFLINE_MESSAGE);
  }
  if (res.status === 401) return toSignIn();
  const session = res.ok ? parseSession(await readJson(res)) : null;
  if (!session) throw new Error(await errorMessage(res));
  return session;
}

export async function signIn(email: string, password: string): Promise<Result<Session>> {
  const res = await post("/api/auth/login", { email: email.trim(), password });
  if (!res) return offline();
  if (res.status === 401) {
    return { ok: false, status: 401, message: await errorMessage(res, "Email or password is not right.") };
  }
  if (!res.ok) return { ok: false, status: res.status, message: await errorMessage(res) };
  const session = parseSession(await readJson(res));
  return session ? { ok: true, value: session } : { ok: false, status: res.status, message: await errorMessage(res) };
}

export interface SignUpInput {
  name: string;
  email: string;
  password: string;
  companyName: string;
  taxId?: string;
}

/** Which field a sign-up refusal is about, so the form can point at it. */
function fieldFor(status: number, body: unknown, message: string): string | undefined {
  const b = isRecord(body) ? body : {};
  if (typeof b.field === "string") return b.field;
  const code = `${typeof b.error === "string" ? b.error : ""} ${message}`.toLowerCase();
  if (status === 409 || /email/.test(code)) return "email";
  if (/password/.test(code)) return "password";
  if (/\bnif\b|tax|vat/.test(code)) return "taxId";
  if (/company/.test(code)) return "companyName";
  if (/name/.test(code)) return "name";
  return undefined;
}

export async function signUp(input: SignUpInput): Promise<Result<Session>> {
  const body: Record<string, string> = {
    email: input.email.trim(),
    password: input.password,
    name: input.name.trim(),
    companyName: input.companyName.trim(),
  };
  const taxId = input.taxId?.trim();
  if (taxId) body.taxId = taxId;
  const res = await post("/api/auth/signup", body);
  if (!res) return offline();
  const parsed = await readJson(res.clone());
  if (!res.ok) {
    const fallback = res.status === 409 ? "There is already an account with this email. Sign in instead." : "I couldn’t create your account. Check the details and try again.";
    const message = await errorMessage(res, fallback);
    return { ok: false, status: res.status, message, field: fieldFor(res.status, parsed, message) };
  }
  const session = parseSession(parsed);
  return session ? { ok: true, value: session } : { ok: false, status: res.status, message: await errorMessage(res) };
}

/** End the session on the server. The cookie goes whatever the network says. */
export async function signOut(): Promise<void> {
  await post("/api/auth/logout", {});
}

/* ---------- Account (GDPR, spec §52) ---------- */

/** Download everything the tenant has, as a zip. Returns null when done, or a message saying why not. */
export function exportAccount(): Promise<string | null> {
  return downloadFile("/api/account/export", "admin2ai-export.zip", "I couldn’t prepare your file. Try again in a moment.");
}

/**
 * Delete the account and all its data. The API re-checks the password, so a
 * 401 or 403 here means the password was not right, not that the session ended.
 */
export async function deleteAccount(password: string): Promise<Result<null>> {
  const res = await post("/api/account/delete", { confirm: "DELETE", password }, 60000);
  if (!res) return offline();
  if (res.ok) return { ok: true, value: null };
  if (res.status === 401 || res.status === 403) {
    return { ok: false, status: res.status, field: "password", message: await errorMessage(res, "That password is not right.") };
  }
  return { ok: false, status: res.status, message: await errorMessage(res, "I couldn’t delete your account. Try again in a moment.") };
}

/* ---------- Onboarding ---------- */

async function write(path: string, body: unknown, fallback: string): Promise<Result<Record<string, unknown>>> {
  const res = await post(path, body);
  if (!res) return offline();
  if (res.status === 401) return toSignIn();
  const parsed = await readJson(res.clone());
  if (!res.ok) {
    const message = await errorMessage(res, fallback);
    return { ok: false, status: res.status, message, field: fieldFor(res.status, parsed, message) };
  }
  return { ok: true, value: isRecord(parsed) ? parsed : {} };
}

export function addCompany(input: { name: string; taxId: string; legalName?: string }) {
  const body: Record<string, string> = { name: input.name.trim(), taxId: input.taxId.trim() };
  if (input.legalName?.trim()) body.legalName = input.legalName.trim();
  return write("/api/onboarding/company", body, "I couldn’t add that company. Check the details and try again.");
}

export function setAccountant(input: { email: string; name?: string }) {
  const body: Record<string, string> = { email: input.email.trim() };
  if (input.name?.trim()) body.name = input.name.trim();
  return write("/api/onboarding/accountant", body, "I couldn’t save your accountant. Try again.");
}

/** How far back the first read of each new mailbox and bank goes (spec §6): POST /api/settings/reading. */
export function chooseHistory(history: "90d" | "12m") {
  return write("/api/settings/reading", { history }, "I couldn’t save that. Try again.");
}

/** Full-page link to the provider's consent screen; the API brings the owner back. */
export function oauthStartUrl(provider: "google" | "microsoft"): string {
  return `${API_URL}/api/oauth/start?provider=${provider}`;
}

/** Only http(s) links leave the app: never javascript: or data: from a reply. */
export function isSafeRedirect(url: string): boolean {
  try {
    const u = new URL(url);
    return u.protocol === "https:" || (u.protocol === "http:" && /^(localhost|127\.0\.0\.1|\[::1\])$/.test(u.hostname));
  } catch {
    return false;
  }
}

/** Ask the API for the bank's consent page (Open Banking). */
export async function startBankConnection(institutionId: string): Promise<Result<string>> {
  const r = await write("/api/connections/bank/start", { institutionId }, "I couldn’t open your bank. Try again in a moment.");
  if (!r.ok) return r;
  const url = r.value.redirectUrl;
  if (typeof url !== "string" || !isSafeRedirect(url)) {
    return { ok: false, status: 502, message: "I couldn’t open your bank. Try again in a moment." };
  }
  return { ok: true, value: url };
}

/** The sign-in page, as a full URL path including the base path. */
export function signInPage(query = ""): string {
  return `${BASE_PATH}/signin${query}`;
}
