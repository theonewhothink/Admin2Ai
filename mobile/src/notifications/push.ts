/**
 * This phone's push notifications (§42: rare, only what matters). Pure: the
 * OS permission and Expo push token come through PushPlatform, the server
 * through DeviceApi, and a couple of small flags through PushPrefs, so every
 * rule here is unit-tested.
 *
 * - Permission is asked once, right after the first sign-in, behind a one-line
 *   reason on our own screen; "Not now" is remembered and never nags.
 * - When permission is granted, the Expo push token is sent to the server
 *   (POST /api/devices). Tokens can change, so it is sent again at each start.
 * - Signing out removes it from the server (POST /api/devices/remove) while
 *   the session still works, and forgets it on the phone.
 */

export type PermissionStatus = "granted" | "denied" | "undetermined";

export interface PushPlatform {
  os: "ios" | "android";
  permission(): Promise<PermissionStatus>;
  request(): Promise<PermissionStatus>;
  /** The Expo push token, or null where there is none (simulator, no EAS project id). */
  expoToken(): Promise<string | null>;
}

export interface DeviceApi {
  registerDevice(expoPushToken: string, platform: "ios" | "android"): Promise<boolean>;
  removeDevice(expoPushToken: string): Promise<boolean>;
}

export interface PushPrefs {
  get(key: string): Promise<string | null>;
  set(key: string, value: string): Promise<void>;
  remove(key: string): Promise<void>;
}

export const PUSH_KEYS = {
  asked: "backoffice.push.asked.v1",
  token: "backoffice.push.token.v1",
} as const;

export type EnableResult = "on" | "denied" | "unavailable";

export class PushRegistration {
  constructor(
    private readonly platform: PushPlatform,
    private readonly api: DeviceApi,
    private readonly prefs: PushPrefs,
  ) {}

  /** Show our one-line reason and ask? Only once, and only if the OS has not been asked. */
  async shouldAsk(): Promise<boolean> {
    if ((await this.prefs.get(PUSH_KEYS.asked).catch(() => null)) === "1") return false;
    return (await this.platform.permission().catch(() => "denied" as const)) === "undetermined";
  }

  /** The owner tapped "Not now". */
  async decline(): Promise<void> {
    await this.prefs.set(PUSH_KEYS.asked, "1").catch(() => undefined);
  }

  /** The owner tapped "Turn on": ask the OS, then tell the server where to reach this phone. */
  async enable(): Promise<EnableResult> {
    await this.prefs.set(PUSH_KEYS.asked, "1").catch(() => undefined);
    let status = await this.platform.permission().catch(() => "denied" as const);
    if (status === "undetermined") status = await this.platform.request().catch(() => "denied" as const);
    if (status !== "granted") return "denied";
    return (await this.register()) ? "on" : "unavailable";
  }

  /** At start, when signed in: keep the server's copy of the token current. Never asks. */
  async sync(): Promise<boolean> {
    const status = await this.platform.permission().catch(() => "denied" as const);
    if (status !== "granted") return false;
    return this.register();
  }

  /** Sign-out: stop notifications to this phone. Best effort; the phone forgets the token either way. */
  async unregister(): Promise<void> {
    const token = await this.prefs.get(PUSH_KEYS.token).catch(() => null);
    if (!token) return;
    await this.api.removeDevice(token).catch(() => false);
    await this.prefs.remove(PUSH_KEYS.token).catch(() => undefined);
  }

  private async register(): Promise<boolean> {
    const token = await this.platform.expoToken().catch(() => null);
    if (!token) return false;
    const previous = await this.prefs.get(PUSH_KEYS.token).catch(() => null);
    const ok = await this.api.registerDevice(token, this.platform.os).catch(() => false);
    if (!ok) return false;
    if (previous && previous !== token) await this.api.removeDevice(previous).catch(() => false);
    await this.prefs.set(PUSH_KEYS.token, token).catch(() => undefined);
    return true;
  }
}
