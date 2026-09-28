/**
 * Creates the real app services (API client, app lock, offline queue) once per
 * JS runtime and starts the queue. Components read them via ./servicesContext.
 */
import { useEffect, useMemo, type ReactNode } from "react";
import { AppState } from "react-native";
import { ApiClient } from "../api/client";
import { SealedSnapshotCache } from "../api/cache";
import { expoHttpSend } from "../api/expoHttp";
import { apiEndpoint } from "../config";
import { registerUploadTask } from "../offline/expo/backgroundTask";
import { ExpoRawFile } from "../offline/expo/files";
import { getOfflineRuntime } from "../offline/expo/runtime";
import { deviceAuthenticator } from "../security/expo";
import { AppLock } from "../security/lock";
import { getSessionToken } from "../security/session";
import { ServicesContext, type Services } from "./servicesContext";

export { useServices, type Services } from "./servicesContext";

function createServices(): Services {
  const offline = getOfflineRuntime();
  const api = new ApiClient({
    endpoint: apiEndpoint(getSessionToken),
    send: expoHttpSend,
    cache: new SealedSnapshotCache(new ExpoRawFile("screens.sealed"), offline.cipher),
  });
  return { api, lock: new AppLock(deviceAuthenticator, Date.now), offline };
}

export function ServicesProvider({ children }: { children: ReactNode }) {
  const services = useMemo(createServices, []);

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
