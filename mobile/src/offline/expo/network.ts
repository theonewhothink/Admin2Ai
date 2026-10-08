/** Network awareness via expo-network (§43: upload when connected). */
import * as Network from "expo-network";
import { isReachable } from "../platform";
import type { NetworkMonitor } from "../types";

export const expoNetwork: NetworkMonitor = {
  async isOnline(): Promise<boolean> {
    return isReachable(await Network.getNetworkStateAsync());
  },
  subscribe(listener: (online: boolean) => void): () => void {
    const subscription = Network.addNetworkStateListener((state) => listener(isReachable(state)));
    return () => subscription.remove();
  },
};
