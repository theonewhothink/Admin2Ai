/**
 * App lock (§52 biometric mobile). The app opens locked and asks for Face ID,
 * Touch ID, fingerprint or the phone passcode. It locks again after a short
 * time in the background. Hard approvals (§25: money movement, bank detail
 * changes) ask again right before they are sent, whatever the lock state.
 */
import { copy } from "../copy";

export type SecurityLevel = "none" | "passcode" | "biometric";

export type AuthFailure = "cancelled" | "failed" | "lockout" | "unavailable";

export type AuthResult = { ok: true } | { ok: false; reason: AuthFailure };

export interface Authenticator {
  securityLevel(): Promise<SecurityLevel>;
  authenticate(prompt: string): Promise<AuthResult>;
}

export type LockState =
  | { status: "locked"; message?: string }
  | { status: "unlocking" }
  | { status: "unlocked" }
  /** The phone has no passcode at all: we cannot lock, so we say so once. */
  | { status: "unprotected" };

export const DEFAULT_GRACE_MS = 60_000;

/** Map expo-local-authentication error codes to the few cases the UI cares about. */
export function mapAuthError(error: string): AuthFailure {
  switch (error) {
    case "user_cancel":
    case "system_cancel":
    case "app_cancel":
    case "user_fallback":
      return "cancelled";
    case "lockout":
      return "lockout";
    case "not_enrolled":
    case "not_available":
    case "passcode_not_set":
      return "unavailable";
    default:
      return "failed";
  }
}

export class AppLock {
  private state: LockState = { status: "locked" };
  private backgroundedAt: number | null = null;
  private readonly listeners = new Set<(state: LockState) => void>();

  constructor(
    private readonly auth: Authenticator,
    private readonly now: () => number,
    private readonly graceMs: number = DEFAULT_GRACE_MS,
  ) {}

  get current(): LockState {
    return this.state;
  }

  get isOpen(): boolean {
    return this.state.status === "unlocked" || this.state.status === "unprotected";
  }

  subscribe(listener: (state: LockState) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /** Ask the owner to unlock. Safe to call repeatedly; concurrent calls are ignored. */
  async unlock(): Promise<LockState> {
    if (this.state.status !== "locked") return this.state;
    this.set({ status: "unlocking" });
    let level: SecurityLevel;
    try {
      level = await this.auth.securityLevel();
    } catch {
      level = "passcode"; // Unknown: still try to authenticate rather than open the app.
    }
    if (level === "none") {
      this.set({ status: "unprotected" });
      return this.state;
    }
    const result = await this.safeAuthenticate(copy.lock.prompt);
    if (result.ok) {
      this.set({ status: "unlocked" });
    } else if (result.reason === "lockout") {
      this.set({ status: "locked", message: copy.lock.lockout });
    } else if (result.reason === "failed") {
      this.set({ status: "locked", message: copy.lock.failed });
    } else {
      this.set({ status: "locked" });
    }
    return this.state;
  }

  onBackground(): void {
    this.backgroundedAt = this.now();
  }

  /** Re-lock if the app stayed in the background longer than the grace period. */
  onForeground(): void {
    const since = this.backgroundedAt;
    this.backgroundedAt = null;
    if (since === null || this.state.status !== "unlocked") return;
    if (this.now() - since > this.graceMs) this.set({ status: "locked" });
  }

  /**
   * Fresh confirmation for a hard approval. On a phone without any passcode we
   * cannot confirm identity; the server still applies its own approval rules.
   */
  async confirm(prompt: string): Promise<boolean> {
    let level: SecurityLevel;
    try {
      level = await this.auth.securityLevel();
    } catch {
      level = "passcode";
    }
    if (level === "none") return true;
    return (await this.safeAuthenticate(prompt)).ok;
  }

  private async safeAuthenticate(prompt: string): Promise<AuthResult> {
    try {
      return await this.auth.authenticate(prompt);
    } catch {
      return { ok: false, reason: "failed" };
    }
  }

  private set(state: LockState): void {
    this.state = state;
    for (const listener of this.listeners) listener(state);
  }
}
