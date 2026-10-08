/**
 * Who is signed in on this phone. Pure (ports for storage and the API), so it
 * is unit-tested without native modules.
 *
 * - Demo mode (no API configured): nobody signs in; the app shows sample data.
 * - Otherwise the token from sign-in is kept in the Keychain / Keystore
 *   (TokenStorage) and sent as `Authorization: Bearer <token>`.
 * - A 401 from any call ends the session on the phone: the token is deleted
 *   and the owner sees the sign-in screen again ("expired").
 * - Signing out stops this phone's notifications first (it needs the session),
 *   ends the session on the server, then deletes the token and forgets the
 *   screens cached for that session. Local cleanup happens even offline.
 *
 * The biometric lock (src/security) stays on top of all of this.
 */

export interface AuthUser {
  id: string;
  email: string;
  name: string;
}

export type AuthState =
  | { status: "demo" }
  | { status: "loading" }
  | { status: "signedOut"; reason: "none" | "expired" | "signedOut" }
  | { status: "signedIn"; user: AuthUser | null; firstSignIn: boolean };

export type SignInOutcome =
  | { ok: true; token: string; user: AuthUser }
  | { ok: false; kind: "invalid" | "credentials" | "rateLimited" | "network" | "server"; message: string };

export interface TokenStorage {
  get(): Promise<string | null>;
  set(token: string): Promise<void>;
  clear(): Promise<void>;
}

export interface AuthApi {
  signIn(email: string, password: string): Promise<SignInOutcome>;
  /** Best effort: never throws. */
  signOut(token: string): Promise<void>;
}

export interface AuthHooks {
  /**
   * After the server accepted the sign-in, before the token is stored or used
   * (e.g. a different owner on this phone: drop the previous owner's unsent
   * documents first, so nothing of theirs is sent with the new token).
   */
  beforeSessionStart?: (user: AuthUser) => Promise<void>;
  /** Before the session ends, while the token still works (e.g. unregister push). */
  beforeSignOut?: () => Promise<void>;
  /** After the session ended for any reason (e.g. forget cached screens). */
  afterSignOut?: () => Promise<void>;
}

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export class AuthStore {
  private current: AuthState;
  private token: string | null = null;
  private readonly listeners = new Set<(state: AuthState) => void>();

  constructor(
    private readonly deps: { storage: TokenStorage; api: AuthApi | null; hooks?: AuthHooks },
  ) {
    this.current = deps.api ? { status: "loading" } : { status: "demo" };
  }

  get state(): AuthState {
    return this.current;
  }

  subscribe(listener: (state: AuthState) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** The token for API calls, or null when signed out (or in demo mode). */
  async getToken(): Promise<string | null> {
    if (this.current.status === "signedIn") return this.token;
    return null;
  }

  /** At app start: is there a saved session? */
  async restore(): Promise<AuthState> {
    if (!this.deps.api) return this.current;
    let token: string | null = null;
    try {
      token = await this.deps.storage.get();
    } catch {
      token = null;
    }
    // A 401 or a sign-in may have happened while the Keychain was being read.
    if (this.current.status !== "loading") return this.current;
    this.token = token;
    this.set(token ? { status: "signedIn", user: null, firstSignIn: false } : { status: "signedOut", reason: "none" });
    return this.current;
  }

  async signIn(email: string, password: string): Promise<SignInOutcome> {
    if (!this.deps.api) return { ok: false, kind: "server", message: "Sign-in is not available in the demo." };
    if (!EMAIL.test(email.trim())) return { ok: false, kind: "invalid", message: "Enter your email address." };
    if (!password) return { ok: false, kind: "invalid", message: "Enter your password." };
    const outcome = await this.deps.api.signIn(email, password);
    if (!outcome.ok) return outcome;
    try {
      await this.deps.hooks?.beforeSessionStart?.(outcome.user);
    } catch {
      // Another owner's documents could not be cleared: never start a session that might send them.
      if (this.deps.api) await this.deps.api.signOut(outcome.token);
      return { ok: false, kind: "server", message: "I couldn't get this phone ready. Try again." };
    }
    try {
      await this.deps.storage.set(outcome.token);
    } catch {
      // The Keychain refused: the session still works until the app closes.
    }
    this.token = outcome.token;
    this.set({ status: "signedIn", user: outcome.user, firstSignIn: true });
    return outcome;
  }

  /** The owner tapped Sign out. */
  async signOut(): Promise<void> {
    if (this.current.status !== "signedIn") return;
    const token = this.token;
    try {
      await this.deps.hooks?.beforeSignOut?.();
    } catch {
      // Unregistering notifications is best effort; the session still ends.
    }
    if (token && this.deps.api) await this.deps.api.signOut(token);
    await this.end("signedOut");
  }

  /** Any call got a 401: the session ended on the server. */
  handleUnauthorized(): void {
    if (this.current.status !== "signedIn") return;
    void this.end("expired");
  }

  private async end(reason: "expired" | "signedOut"): Promise<void> {
    this.token = null;
    this.set({ status: "signedOut", reason });
    try {
      await this.deps.storage.clear();
    } catch {
      // Nothing stored, or the Keychain is unavailable: the token is gone from memory.
    }
    try {
      await this.deps.hooks?.afterSignOut?.();
    } catch {
      // Cleanup is best effort.
    }
  }

  private set(next: AuthState) {
    this.current = next;
    for (const l of [...this.listeners]) l(next);
  }
}
