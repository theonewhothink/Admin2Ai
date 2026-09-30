/**
 * Creates the real app services (API client, sign-in, notifications, app lock,
 * offline queue) once per JS runtime and starts the queue. Components read
 * them via ./servicesContext.
 */
import { useEffect, useMemo, type ReactNode } from "react";
import { AppState } from "react-native";
import { ApiClient } from "../api/client";
import { SealedSnapshotCache } from "../api/cache";
import { expoHttpSend } from "../api/expoHttp";
import { httpAuthApi } from "../auth/api";
import { onUnauthorized } from "../auth/events";
import { ownerSwitchGuard } from "../auth/ownerSwitch";
import { AuthStore } from "../auth/store";
import { API_BASE_URL, API_TIMEOUT_MS, apiEndpoint } from "../config";
import { configureNotifications, expoPushPlatform } from "../notifications/expo";
import { PushRegistration } from "../notifications/push";
import { registerUploadTask } from "../offline/expo/backgroundTask";
import { ExpoRawFile } from "../offline/expo/files";
import { getOfflineRuntime } from "../offline/expo/runtime";
import { deviceAuthenticator } from "../security/expo";
import { AppLock } from "../security/lock";
import { securePrefs, secureTokenStorage } from "../security/session";
import { ServicesContext, type Services } from "./servicesContext";

export { useServices, type Services } from "./servicesContext";

function createServices(): Services {
  const offline = getOfflineRuntime();
  // The hooks run only on sign-out, long after `api` and `push` below exist.
  const auth: AuthStore = new AuthStore({
    storage: secureTokenStorage,
    api: API_BASE_URL ? httpAuthApi({ baseUrl: API_BASE_URL, timeoutMs: API_TIMEOUT_MS, send: expoHttpSend }) : null,
    hooks: {
      // A different owner on this phone: their predecessor's unsent documents and cached screens go first.
      beforeSessionStart: (user) =>
        ownerSwitchGuard({
          prefs: securePrefs,
          discardQueue: () => offline.pipeline.discardAll(),
          forgetScreens: () => api.forgetCachedScreens(),
        })(user),
      beforeSignOut: async () => {
        await push?.unregister();
      },
      afterSignOut: () => api.forgetCachedScreens(),
    },
  });
  const api = new ApiClient({
    endpoint: apiEndpoint(() => auth.getToken()),
    send: expoHttpSend,
    cache: new SealedSnapshotCache(new ExpoRawFile("screens.sealed"), offline.cipher),
  });
  const push = API_BASE_URL ? new PushRegistration(expoPushPlatform, api, securePrefs) : null;
  if (push) configureNotifications();
  return { api, lock: new AppLock(deviceAuthenticator, Date.now), offline, auth, push };
}

export function ServicesProvider({ children }: { children: ReactNode }) {
  const services = useMemo(createServices, []);

  useEffect(() => {
    const { auth, offline } = services;
    // Any 401 (screens or uploads) ends the session on the phone: back to sign-in.
    const stop = onUnauthorized(() => auth.handleUnauthorized());
    // Documents that waited for a session go as soon as there is one.
    const unsubscribe = auth.subscribe((state) => {
      if (state.status === "signedIn") void offline.runner.kick();
    });
    void auth.restore();
    return () => {
      stop();
      unsubscribe();
    };
  }, [services]);

  useEffect(() => {
    const { pipeline, runner } = services.offline;
    let cancelled = false;
    let stop: (() => void) | null = null;
    // Repair anything interrupted last time, then keep the queue moving (§43).
    void pipeline
      .recover()
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) stop = runner.start();
      });
    void registerUploadTask().catch(() => false);
    const sub = AppState.addEventListener("change", (state) => {
      if (state === "active") void runner.kick();
    });
    return () => {
      cancelled = true;
      sub.remove();
      stop?.();
    };
  }, [services]);

  return <ServicesContext.Provider value={services}>{children}</ServicesContext.Provider>;
}
