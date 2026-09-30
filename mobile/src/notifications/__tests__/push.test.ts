import { describe, expect, it, jest } from "@jest/globals";
import { PUSH_KEYS, PushRegistration, type DeviceApi, type PermissionStatus, type PushPlatform, type PushPrefs } from "../push";
import { routeForNotification } from "../route";

function setup(options: { permission?: PermissionStatus; afterRequest?: PermissionStatus; token?: string | null; serverOk?: boolean; prefs?: Record<string, string> } = {}) {
  let permission: PermissionStatus = options.permission ?? "undetermined";
  const platform: PushPlatform = {
    os: "ios",
    permission: jest.fn(async () => permission),
    request: jest.fn(async () => {
      permission = options.afterRequest ?? "granted";
      return permission;
    }),
    expoToken: jest.fn(async () => (options.token === undefined ? "ExponentPushToken[new]" : options.token)),
  };
  const api = {
    registerDevice: jest.fn(async (_t: string, _p: "ios" | "android") => options.serverOk ?? true),
    removeDevice: jest.fn(async (_t: string) => true),
  } satisfies DeviceApi;
  const store = new Map(Object.entries(options.prefs ?? {}));
  const prefs: PushPrefs = {
    get: async (k) => store.get(k) ?? null,
    set: async (k, v) => void store.set(k, v),
    remove: async (k) => void store.delete(k),
  };
  return { push: new PushRegistration(platform, api, prefs), platform, api, store };
}

describe("asking for permission", () => {
  it("asks once, after the first sign-in, only if the phone has not been asked", async () => {
    expect(await setup().push.shouldAsk()).toBe(true);
    expect(await setup({ permission: "granted" }).push.shouldAsk()).toBe(false);
    expect(await setup({ permission: "denied" }).push.shouldAsk()).toBe(false);
    expect(await setup({ prefs: { [PUSH_KEYS.asked]: "1" } }).push.shouldAsk()).toBe(false);
  });

  it("'Not now' is remembered and the OS prompt never shows", async () => {
    const { push, platform, store } = setup();
    await push.decline();
    expect(store.get(PUSH_KEYS.asked)).toBe("1");
    expect(await push.shouldAsk()).toBe(false);
    expect(platform.request).not.toHaveBeenCalled();
  });

  it("'Turn on' asks the OS, then registers the Expo token with the server", async () => {
    const { push, platform, api, store } = setup();
    expect(await push.enable()).toBe("on");
    expect(platform.request).toHaveBeenCalledTimes(1);
    expect(api.registerDevice).toHaveBeenCalledWith("ExponentPushToken[new]", "ios");
    expect(store.get(PUSH_KEYS.token)).toBe("ExponentPushToken[new]");
  });

  it("says why nothing happened: refused, or no token on this phone", async () => {
    expect(await setup({ afterRequest: "denied" }).push.enable()).toBe("denied");
    expect(await setup({ token: null }).push.enable()).toBe("unavailable");
    const failing = setup({ serverOk: false });
    expect(await failing.push.enable()).toBe("unavailable");
    expect(failing.store.has(PUSH_KEYS.token)).toBe(false);
  });
});

describe("keeping the server current", () => {
  it("re-sends the token at start when allowed, never prompting", async () => {
    const { push, platform, api } = setup({ permission: "granted" });
    expect(await push.sync()).toBe(true);
    expect(platform.request).not.toHaveBeenCalled();
    expect(api.registerDevice).toHaveBeenCalledTimes(1);
    const off = setup({ permission: "undetermined" });
    expect(await off.push.sync()).toBe(false);
    expect(off.platform.request).not.toHaveBeenCalled();
  });

  it("replaces a changed token on the server", async () => {
    const { push, api, store } = setup({ permission: "granted", prefs: { [PUSH_KEYS.token]: "ExponentPushToken[old]" } });
    await push.sync();
    expect(api.removeDevice).toHaveBeenCalledWith("ExponentPushToken[old]");
    expect(store.get(PUSH_KEYS.token)).toBe("ExponentPushToken[new]");
  });

  it("sign-out removes this phone from the server and forgets the token", async () => {
    const { push, api, store } = setup({ prefs: { [PUSH_KEYS.token]: "ExponentPushToken[abc]" } });
    await push.unregister();
    expect(api.removeDevice).toHaveBeenCalledWith("ExponentPushToken[abc]");
    expect(store.has(PUSH_KEYS.token)).toBe(false);
    const none = setup();
    await none.push.unregister();
    expect(none.api.removeDevice).not.toHaveBeenCalled();
  });
});

describe("tapping a notification", () => {
  it("opens Needs you for approvals and changed bank details", () => {
    expect(routeForNotification({ type: "hard_approval" })).toBe("/needs-you");
    expect(routeForNotification({ kind: "bank-details-changed" })).toBe("/needs-you");
    expect(routeForNotification({ type: "payment_approval", itemId: "nd_1" })).toBe("/needs-you");
    expect(routeForNotification({ screen: "needs-you" })).toBe("/needs-you");
  });

  it("opens Connections for a connection that needs reconnecting", () => {
    expect(routeForNotification({ type: "connection_stale" })).toBe("/connections");
    expect(routeForNotification({ url: "backoffice://connections" })).toBe("/connections");
    expect(routeForNotification({ url: "https://app.admin2ai.eu/connections?id=gmail" })).toBe("/connections");
  });

  it("opens Home for a closed month, and nothing for unknown payloads", () => {
    expect(routeForNotification({ type: "month_closed" })).toBe("/");
    expect(routeForNotification({ url: "/" })).toBe("/");
    expect(routeForNotification({ type: "promo" })).toBeNull();
    expect(routeForNotification({ url: "javascript:alert(1)" })).toBeNull();
    expect(routeForNotification(null)).toBeNull();
    expect(routeForNotification("needs-you")).toBeNull();
  });
});
