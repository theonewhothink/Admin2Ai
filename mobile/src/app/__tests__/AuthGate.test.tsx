import { describe, expect, it, jest } from "@jest/globals";
import { act, fireEvent, render, screen } from "@testing-library/react-native";
import { Text } from "react-native";
import { AuthStore, type AuthApi, type TokenStorage } from "../../auth/store";
import { PushRegistration, type PushPrefs } from "../../notifications/push";
import { AuthGate } from "../AuthGate";
import { ServicesContext, type Services } from "../servicesContext";

jest.mock("react-native-safe-area-context", () => require("react-native-safe-area-context/jest/mock").default);

const user = { id: "u1", email: "laura@example.pt", name: "Laura" };

function setup(options: { token?: string | null; api?: AuthApi | null; permission?: "undetermined" | "granted" } = {}) {
  let saved = options.token ?? null;
  const storage: TokenStorage = {
    get: async () => saved,
    set: async (t) => void (saved = t),
    clear: async () => void (saved = null),
  };
  const api: AuthApi | null =
    options.api === undefined
      ? {
          signIn: async (_e, password) =>
            password === "correct horse battery" ? { ok: true, token: "tok-1", user } : { ok: false, kind: "credentials", message: "Email or password is not right." },
          signOut: async () => undefined,
        }
      : options.api;
  const auth = new AuthStore({ storage, api });
  const prefs = new Map<string, string>();
  const pushPrefs: PushPrefs = { get: async (k) => prefs.get(k) ?? null, set: async (k, v) => void prefs.set(k, v), remove: async (k) => void prefs.delete(k) };
  const registerDevice = jest.fn(async () => true);
  const permission = options.permission ?? "undetermined";
  const push = api
    ? new PushRegistration(
        { os: "android", permission: async () => permission, request: async () => "granted", expoToken: async () => "ExponentPushToken[x]" },
        { registerDevice, removeDevice: async () => true },
        pushPrefs,
      )
    : null;
  const services = { api: {}, lock: {}, offline: {}, auth, push } as unknown as Services;
  const ui = () =>
    render(
      <ServicesContext.Provider value={services}>
        <AuthGate>
          <Text>The app</Text>
        </AuthGate>
      </ServicesContext.Provider>,
    );
  return { auth, ui, registerDevice, storage: () => saved };
}

describe("sign-in gate", () => {
  it("demo mode shows the app with no sign-in", async () => {
    const { ui } = setup({ api: null });
    await ui();
    expect(screen.getByText("The app")).toBeTruthy();
  });

  it("signed out: the sign-in screen, never the app", async () => {
    const { auth, ui } = setup();
    await auth.restore();
    await ui();
    expect(screen.queryByText("The app")).toBeNull();
    expect(screen.getByLabelText("Email")).toBeTruthy();
    expect(screen.getByLabelText("Password")).toBeTruthy();
  });

  it("wrong password says so plainly; the right one asks once about notifications, then opens the app", async () => {
    const { auth, ui, registerDevice, storage } = setup();
    await auth.restore();
    await ui();
    await fireEvent.changeText(screen.getByLabelText("Email"), "laura@example.pt");
    await fireEvent.changeText(screen.getByLabelText("Password"), "nope");
    await fireEvent.press(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Email or password is not right.")).toBeTruthy();

    await fireEvent.changeText(screen.getByLabelText("Password"), "correct horse battery");
    await fireEvent.press(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Turn on notifications?")).toBeTruthy();
    expect(screen.getByText("I only notify you when I need a decision, a connection stops working, or a month is closed.")).toBeTruthy();
    expect(storage()).toBe("tok-1");

    await fireEvent.press(screen.getByRole("button", { name: "Turn on" }));
    expect(await screen.findByText("The app")).toBeTruthy();
    expect(registerDevice).toHaveBeenCalledWith("ExponentPushToken[x]", "android");
  });

  it("a restored session opens the app at once and keeps the push token current", async () => {
    const { auth, ui, registerDevice } = setup({ token: "tok-saved", permission: "granted" });
    await auth.restore();
    await ui();
    expect(screen.getByText("The app")).toBeTruthy();
    await act(async () => undefined);
    expect(registerDevice).toHaveBeenCalledTimes(1);
  });

  it("a 401 anywhere returns to sign-in and says why", async () => {
    const { auth, ui, storage } = setup({ token: "tok-saved" });
    await auth.restore();
    await ui();
    expect(screen.getByText("The app")).toBeTruthy();
    await act(async () => {
      auth.handleUnauthorized();
    });
    expect(screen.queryByText("The app")).toBeNull();
    expect(screen.getByText("You were signed out. Sign in again to continue.")).toBeTruthy();
    expect(storage()).toBeNull();
  });
});
