import { describe, expect, it } from "@jest/globals";
import { AppLock, mapAuthError, type AuthResult, type Authenticator, type LockState, type SecurityLevel } from "../lock";

function fakeAuth(level: SecurityLevel, results: AuthResult[]): Authenticator & { prompts: string[] } {
  const prompts: string[] = [];
  return {
    prompts,
    securityLevel: async () => level,
    authenticate: async (prompt) => {
      prompts.push(prompt);
      return results.shift() ?? { ok: true };
    },
  };
}

describe("AppLock", () => {
  it("opens locked and unlocks after a successful check", async () => {
    const lock = new AppLock(fakeAuth("biometric", [{ ok: true }]), () => 0);
    const seen: LockState["status"][] = [];
    lock.subscribe((s) => seen.push(s.status));
    expect(lock.current.status).toBe("locked");
    await lock.unlock();
    expect(lock.isOpen).toBe(true);
    expect(seen).toEqual(["unlocking", "unlocked"]);
  });

  it("stays locked when cancelled, quietly", async () => {
    const lock = new AppLock(fakeAuth("biometric", [{ ok: false, reason: "cancelled" }]), () => 0);
    expect(await lock.unlock()).toEqual({ status: "locked" });
  });

  it("explains a failure or lockout in plain words", async () => {
    const lock = new AppLock(fakeAuth("passcode", [{ ok: false, reason: "failed" }, { ok: false, reason: "lockout" }]), () => 0);
    expect(await lock.unlock()).toEqual({ status: "locked", message: "That didn't work. Try again." });
    expect(await lock.unlock()).toEqual({ status: "locked", message: "Too many attempts. Unlock your phone first, then try again." });
  });

  it("treats a thrown authenticator as a failed attempt, never as success", async () => {
    const auth: Authenticator = {
      securityLevel: async () => "biometric",
      authenticate: async () => {
        throw new Error("native crash");
      },
    };
    const lock = new AppLock(auth, () => 0);
    expect((await lock.unlock()).status).toBe("locked");
    expect(await lock.confirm("x")).toBe(false);
  });

  it("lets the owner in on a phone without any passcode, flagged as unprotected", async () => {
    const lock = new AppLock(fakeAuth("none", []), () => 0);
    expect(await lock.unlock()).toEqual({ status: "unprotected" });
    expect(lock.isOpen).toBe(true);
  });

  it("re-locks only after the grace period in the background", async () => {
    let t = 0;
    const lock = new AppLock(fakeAuth("biometric", []), () => t, 60_000);
    await lock.unlock();
    lock.onBackground();
    t = 30_000;
    lock.onForeground();
    expect(lock.current.status).toBe("unlocked");
    lock.onBackground();
    t = 100_000;
    lock.onForeground();
    expect(lock.current.status).toBe("locked");
  });

  it("asks again for hard approvals, even when unlocked", async () => {
    const auth = fakeAuth("biometric", [{ ok: true }, { ok: false, reason: "cancelled" }]);
    const lock = new AppLock(auth, () => 0);
    await lock.unlock();
    expect(await lock.confirm("Confirm it's you")).toBe(false);
    expect(auth.prompts).toEqual(["Unlock Back Office", "Confirm it's you"]);
  });

  it("maps platform error codes", () => {
    expect(mapAuthError("user_cancel")).toBe("cancelled");
    expect(mapAuthError("lockout")).toBe("lockout");
    expect(mapAuthError("passcode_not_set")).toBe("unavailable");
    expect(mapAuthError("authentication_failed")).toBe("failed");
  });
});
