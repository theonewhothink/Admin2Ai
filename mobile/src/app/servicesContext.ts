/** Services context and hook, free of native imports so components render in tests. */
import { createContext, useContext } from "react";
import type { ApiClient } from "../api/client";
import type { OfflineRuntime } from "../offline/expo/runtime";
import type { AppLock } from "../security/lock";

export interface Services {
  api: ApiClient;
  lock: AppLock;
  offline: OfflineRuntime;
}

export const ServicesContext = createContext<Services | null>(null);

export function useServices(): Services {
  const services = useContext(ServicesContext);
  if (!services) throw new Error("useServices must be used inside ServicesProvider");
  return services;
}
