/**
 * One phone, one owner at a time. When a different owner signs in than the
 * last one, the previous owner's unsent documents and cached screens are
 * removed before the new session starts, so nothing of theirs reaches the new
 * owner's account. The same owner signing in again keeps everything (unsent
 * documents then go as promised at sign-out). Pure: ports only.
 */
import type { AuthUser } from "./store";

export const LAST_OWNER_KEY = "backoffice.last-owner.v1";

export interface OwnerSwitchDeps {
  prefs: { get(key: string): Promise<string | null>; set(key: string, value: string): Promise<void> };
  discardQueue: () => Promise<unknown>;
  forgetScreens: () => Promise<void>;
}

/** The AuthStore `beforeSessionStart` hook. Throws if the previous owner's data could not be removed. */
export function ownerSwitchGuard(deps: OwnerSwitchDeps): (user: AuthUser) => Promise<void> {
  return async (user) => {
    if (!user.id) return;
    const last = await deps.prefs.get(LAST_OWNER_KEY).catch(() => null);
    if (last && last !== user.id) {
      await deps.discardQueue();
      await deps.forgetScreens();
    }
    await deps.prefs.set(LAST_OWNER_KEY, user.id).catch(() => undefined);
  };
}
