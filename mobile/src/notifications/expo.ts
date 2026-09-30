/**
 * expo-notifications behind the PushPlatform port, plus tap handling.
 * The Expo push token needs the EAS project id (app.config.ts → extra.eas.projectId).
 */
import Constants from "expo-constants";
import * as Notifications from "expo-notifications";
import { useEffect } from "react";
import { Platform } from "react-native";
import type { PermissionStatus, PushPlatform } from "./push";
import { routeForNotification, type NotificationTarget } from "./route";

/** Notifications are rare and matter (§42): show them even while the app is open, quietly. */
export function configureNotifications(): void {
  Notifications.setNotificationHandler({
    handleNotification: async () => ({
      shouldShowBanner: true,
      shouldShowList: true,
      shouldPlaySound: false,
      shouldSetBadge: false,
    }),
  });
  if (Platform.OS === "android") {
    void Notifications.setNotificationChannelAsync("default", {
      name: "Important",
      importance: Notifications.AndroidImportance.HIGH,
      lightColor: "#111318",
    }).catch(() => undefined);
  }
}

function toStatus(p: Notifications.NotificationPermissionsStatus): PermissionStatus {
  if (p.granted || p.ios?.status === Notifications.IosAuthorizationStatus.PROVISIONAL) return "granted";
  // Android reports "denied" before the first request on some versions; what matters is whether we may ask.
  return p.canAskAgain ? "undetermined" : "denied";
}

function projectId(): string | null {
  const extra = Constants.expoConfig?.extra as { eas?: { projectId?: unknown } } | undefined;
  const id = extra?.eas?.projectId ?? Constants.easConfig?.projectId;
  return typeof id === "string" && id ? id : null;
}

export const expoPushPlatform: PushPlatform = {
  os: Platform.OS === "ios" ? "ios" : "android",
  async permission() {
    return toStatus(await Notifications.getPermissionsAsync());
  },
  async request() {
    return toStatus(await Notifications.requestPermissionsAsync({ ios: { allowAlert: true, allowBadge: false, allowSound: true } }));
  },
  async expoToken() {
    const id = projectId();
    if (!id) return null;
    const token = await Notifications.getExpoPushTokenAsync({ projectId: id });
    return token.data || null;
  },
};

// A response is acted on once per app run, even if the listener mounts again (e.g. after signing in).
const handled = new Set<string>();

/** Open the matching screen when the owner taps a notification, including the one that launched the app. */
export function useNotificationTaps(open: (target: NotificationTarget) => void): void {
  useEffect(() => {
    const handle = (response: Notifications.NotificationResponse) => {
      const id = response.notification.request.identifier;
      if (handled.has(id)) return;
      handled.add(id);
      const target = routeForNotification(response.notification.request.content.data);
      if (target) open(target);
    };
    const last = Notifications.getLastNotificationResponse();
    if (last) handle(last);
    const sub = Notifications.addNotificationResponseReceivedListener(handle);
    return () => sub.remove();
  }, [open]);
}
