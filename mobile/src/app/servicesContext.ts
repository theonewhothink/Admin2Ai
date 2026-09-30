/** Services context and hook, free of native imports so components render in tests. */
import { createContext, useContext, useSyncExternalStore } from "react";
import type { ApiClient } from "../api/client";
import type { AuthState, AuthStore } from "../auth/store";
import type { PushRegistration } from "../notifications/push";
import type { OfflineRuntime } from "../offline/expo/runtime";
import type { AppLock } from "../security/lock";

export interface Services {
  api: ApiClient;
  lock: AppLock;
  offline: OfflineRuntime;
  auth: AuthStore;
  /** Null in demo mode: there is no server to notify this phone. */
  push: PushRegistration | null;
}

export const ServicesContext = createContext<Services | null>(null);

export function useServices(): Services {
  const services = useContext(ServicesContext);
  if (!services) throw new Error("useServices must be used inside ServicesProvider");
  return services;
}

/** The current sign-in state; re-renders when it changes. */
export function useAuthState(auth: AuthStore): AuthState {
  return useSyncExternalStore(
    (listener) => auth.subscribe(listener),
    () => auth.state,
  );
}
