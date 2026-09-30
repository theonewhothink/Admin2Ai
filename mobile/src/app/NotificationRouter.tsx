/** Tapping a notification opens its screen (needs-you, connections, home). Mounted only when signed in. */
import { useRouter } from "expo-router";
import { useCallback } from "react";
import { useNotificationTaps } from "../notifications/expo";
import type { NotificationTarget } from "../notifications/route";

export function NotificationRouter() {
  const router = useRouter();
  const open = useCallback((target: NotificationTarget) => router.navigate(target), [router]);
  useNotificationTaps(open);
  return null;
}
