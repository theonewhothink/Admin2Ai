/**
 * Root layout: fonts, share intents (§12), services, the biometric lock (§52)
 * on top of sign-in, notification taps (§42) and the background upload task
 * (§43).
 */
// Registers the background upload task at module load (TaskManager requirement).
import "../src/offline/expo/backgroundTask";

import { Geist_400Regular } from "@expo-google-fonts/geist/400Regular";
import { Geist_500Medium } from "@expo-google-fonts/geist/500Medium";
import { Geist_600SemiBold } from "@expo-google-fonts/geist/600SemiBold";
import { useFonts } from "expo-font";
import { SplashScreen, Stack } from "expo-router";
import { ShareIntentProvider } from "expo-share-intent";
import { StatusBar } from "expo-status-bar";
import { useEffect } from "react";
import { SafeAreaProvider } from "react-native-safe-area-context";
import { AuthGate } from "../src/app/AuthGate";
import { LockGate } from "../src/app/LockGate";
import { NeedsProvider } from "../src/app/needs";
import { NotificationRouter } from "../src/app/NotificationRouter";
import { ServicesProvider } from "../src/app/services";
import { ShareHandler } from "../src/app/ShareHandler";
import { REQUIRES_SIGN_IN } from "../src/config";
import { colors } from "../src/theme/tokens";

void SplashScreen.preventAutoHideAsync();

export default function RootLayout() {
  const [fontsLoaded, fontError] = useFonts({ Geist_400Regular, Geist_500Medium, Geist_600SemiBold });
  const ready = fontsLoaded || fontError !== null;

  useEffect(() => {
    // If the font fails to load, the system font is used rather than blocking the app.
    if (ready) void SplashScreen.hideAsync();
  }, [ready]);

  if (!ready) return null;
  return (
    <ShareIntentProvider>
      <SafeAreaProvider>
        <ServicesProvider>
          <LockGate>
            <StatusBar style="dark" />
            <AuthGate>
              <NeedsProvider>
                <Stack screenOptions={{ headerShown: false, contentStyle: { backgroundColor: colors.bg } }} />
                <ShareHandler />
                {REQUIRES_SIGN_IN ? <NotificationRouter /> : null}
              </NeedsProvider>
            </AuthGate>
          </LockGate>
        </ServicesProvider>
      </SafeAreaProvider>
    </ShareIntentProvider>
  );
}
