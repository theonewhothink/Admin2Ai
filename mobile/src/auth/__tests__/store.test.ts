import { describe, expect, it, jest } from "@jest/globals";
import { LAST_OWNER_KEY, ownerSwitchGuard } from "../ownerSwitch";
import { AuthStore, type AuthApi, type AuthState, type SignInOutcome, type TokenStorage } from "../store";

function memoryStorage(initial: string | null = null) {
  let value = initial;
  const storage: TokenStorage & { value: () => string | null } = {
    get: jest.fn(async () => value),
    set: jest.fn(async (t: string) => {
      value = t;
    }),
    clear: jest.fn(async () => {
      value = null;
    }),
    value: () => value,
  };
  return storage;
}

const user = { id: "u1", email: "laura@example.pt", name: "Laura Martins" };

function fakeApi(outcome: SignInOutcome = { ok: true, token: "tok-1", user }) {
  const api = {
    signIn: jest.fn(async (_email: string, _password: string) => outcome),
    signOut: jest.fn(async (_token: string) => undefined),
  } satisfies AuthApi;
  return api;
}

function statuses(store: AuthStore): AuthState[] {
  const seen: AuthState[] = [];
  store.subscribe((s) => seen.push(s));
  return seen;
}

describe("demo mode (no server)", () => {
  it("never asks anyone to sign in and sends no token", async () => {
    const store = new AuthStore({ storage: memoryStorage("stale"), api: null });
    expect(store.state).toEqual({ status: "demo" });
    expect(await store.restore()).toEqual({ status: "demo" });
    expect(await store.getToken()).toBeNull();
  });
});

describe("restore", () => {
  it("is signed in when the Keychain has a token", async () => {
    const store = new AuthStore({ storage: memoryStorage("tok-saved"), api: fakeApi() });
    expect(store.state.status).toBe("loading");
    expect(await store.restore()).toEqual({ status: "signedIn", user: null, firstSignIn: false });
    expect(await store.getToken()).toBe("tok-saved");
  });

  it("is signed out when there is no token, or the Keychain fails", async () => {
    expect(await new AuthStore({ storage: memoryStorage(null), api: fakeApi() }).restore()).toEqual({ status: "signedOut", reason: "none" });
    const broken: TokenStorage = { get: async () => Promise.reject(new Error("locked")), set: async () => undefined, clear: async () => undefined };
    expect(await new AuthStore({ storage: broken, api: fakeApi() }).restore()).toEqual({ status: "signedOut", reason: "none" });
  });
});

describe("sign in", () => {
  it("stores the token in the Keychain and marks the first sign-in", async () => {
    const storage = memoryStorage();
    const api = fakeApi();
    const store = new AuthStore({ storage, api });
    await store.restore();
    const outcome = await store.signIn(" laura@example.pt ", "correct horse battery");
    expect(outcome.ok).toBe(true);
    expect(api.signIn).toHaveBeenCalledWith(" laura@example.pt ", "correct horse battery");
    expect(storage.value()).toBe("tok-1");
    expect(store.state).toEqual({ status: "signedIn", user, firstSignIn: true });
    expect(await store.getToken()).toBe("tok-1");
  });

  it("checks the form before calling the server", async () => {
    const api = fakeApi();
    const store = new AuthStore({ storage: memoryStorage(), api });
    expect(await store.signIn("not-an-email", "x")).toMatchObject({ ok: false, kind: "invalid", message: "Enter your email address." });
    expect(await store.signIn("laura@example.pt", "")).toMatchObject({ ok: false, kind: "invalid", message: "Enter your password." });
    expect(api.signIn).not.toHaveBeenCalled();
  });

  it("stays signed out on wrong credentials and stores nothing", async () => {
    const storage = memoryStorage();
    const store = new AuthStore({
      storage,
      api: fakeApi({ ok: false, kind: "credentials", message: "Email or password is not right." }),
    });
    await store.restore();
    const outcome = await store.signIn("laura@example.pt", "wrong password");
    expect(outcome).toEqual({ ok: false, kind: "credentials", message: "Email or password is not right." });
    expect(store.state).toEqual({ status: "signedOut", reason: "none" });
    expect(storage.set).not.toHaveBeenCalled();
    expect(await store.getToken()).toBeNull();
  });
});

describe("a different owner on the same phone", () => {
  function prefs(initial: Record<string, string> = {}) {
    const map = new Map(Object.entries(initial));
    return { map, get: async (k: string) => map.get(k) ?? null, set: async (k: string, v: string) => void map.set(k, v) };
  }

  it("clears the previous owner's unsent documents and screens before the new token exists", async () => {
    const storage = memoryStorage();
    const order: string[] = [];
    const p = prefs({ [LAST_OWNER_KEY]: "someone-else" });
    const guard = ownerSwitchGuard({
      prefs: p,
      discardQueue: async () => order.push(`discard (token stored: ${storage.value() !== null})`),
      forgetScreens: async () => void order.push("forget"),
    });
    const store = new AuthStore({ storage, api: fakeApi(), hooks: { beforeSessionStart: guard } });
    await store.restore();
    await store.signIn("laura@example.pt", "correct horse battery");
    expect(order).toEqual(["discard (token stored: false)", "forget"]);
    expect(p.map.get(LAST_OWNER_KEY)).toBe("u1");
    expect(storage.value()).toBe("tok-1");
  });

  it("keeps everything for the same owner, and for the first owner on the phone", async () => {
    const cases: Record<string, string>[] = [{ [LAST_OWNER_KEY]: "u1" }, {}];
    for (const initial of cases) {
      const discardQueue = jest.fn(async () => 0);
      const guard = ownerSwitchGuard({ prefs: prefs(initial), discardQueue, forgetScreens: async () => undefined });
      await guard(user);
      expect(discardQueue).not.toHaveBeenCalled();
    }
  });

  it("never starts the session if the previous owner's documents could not be removed", async () => {
    const storage = memoryStorage();
    const api = fakeApi();
    const guard = ownerSwitchGuard({
      prefs: prefs({ [LAST_OWNER_KEY]: "someone-else" }),
      discardQueue: async () => Promise.reject(new Error("disk full")),
      forgetScreens: async () => undefined,
    });
    const store = new AuthStore({ storage, api, hooks: { beforeSessionStart: guard } });
    await store.restore();
    const outcome = await store.signIn("laura@example.pt", "correct horse battery");
    expect(outcome).toMatchObject({ ok: false, kind: "server" });
    expect(storage.value()).toBeNull();
    expect(api.signOut).toHaveBeenCalledWith("tok-1");
    expect(store.state.status).toBe("signedOut");
  });
});

describe("sign out", () => {
  it("stops notifications while the token still works, ends the session, then forgets everything", async () => {
    const storage = memoryStorage("tok-saved");
    const api = fakeApi();
    const order: string[] = [];
    const store = new AuthStore({
      storage,
      api,
      hooks: {
        beforeSignOut: async () => {
          order.push(`before:${await store.getToken()}`);
        },
        afterSignOut: async () => {
          order.push(`after:${await store.getToken()}`);
        },
      },
    });
    await store.restore();
    api.signOut.mockImplementation(async (token: string) => {
      order.push(`logout:${token}`);
    });
    await store.signOut();
    expect(order).toEqual(["before:tok-saved", "logout:tok-saved", "after:null"]);
    expect(storage.value()).toBeNull();
    expect(store.state).toEqual({ status: "signedOut", reason: "signedOut" });
  });

  it("still signs out on the phone when offline or when a hook fails", async () => {
    const storage = memoryStorage("tok-saved");
    const api = fakeApi();
    api.signOut.mockImplementation(async () => undefined); // httpAuthApi swallows network errors
    const store = new AuthStore({
      storage,
      api,
      hooks: { beforeSignOut: async () => Promise.reject(new Error("offline")) },
    });
    await store.restore();
    await store.signOut();
    expect(storage.value()).toBeNull();
    expect(store.state.status).toBe("signedOut");
  });
});

describe("401 handling", () => {
  it("sends the owner back to sign-in with 'expired', once", async () => {
    const storage = memoryStorage("tok-saved");
    const afterSignOut = jest.fn(async () => undefined);
    const store = new AuthStore({ storage, api: fakeApi(), hooks: { afterSignOut } });
    await store.restore();
    const seen = statuses(store);
    store.handleUnauthorized();
    store.handleUnauthorized();
    await Promise.resolve();
    await Promise.resolve();
    expect(seen).toEqual([{ status: "signedOut", reason: "expired" }]);
    expect(await store.getToken()).toBeNull();
    expect(storage.value()).toBeNull();
    expect(afterSignOut).toHaveBeenCalledTimes(1);
  });

  it("does nothing when nobody is signed in", async () => {
    const store = new AuthStore({ storage: memoryStorage(), api: fakeApi() });
    await store.restore();
    const seen = statuses(store);
    store.handleUnauthorized();
    expect(seen).toEqual([]);
  });

  it("a 401 while the Keychain is still being read wins over the old token", async () => {
    let release: (v: string | null) => void = () => undefined;
    const slow: TokenStorage = {
      get: () => new Promise<string | null>((r) => (release = r)),
      set: async () => undefined,
      clear: async () => undefined,
    };
    const store = new AuthStore({ storage: slow, api: fakeApi() });
    const restoring = store.restore();
    await store.signIn("laura@example.pt", "correct horse battery");
    release("old-token");
    expect(await restoring).toEqual({ status: "signedIn", user, firstSignIn: true });
    expect(await store.getToken()).toBe("tok-1");
  });
});
