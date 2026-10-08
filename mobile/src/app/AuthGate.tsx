/**
 * Sign-in gate (production). Inside the biometric lock, which stays on top.
 *
 * - Demo mode: the app as before, no sign-in.
 * - Signed out: the sign-in screen instead of the app, so no screen loads data
 *   without a session. After signing in the app mounts fresh.
 * - Right after the first sign-in: one screen asking to turn on notifications
 *   (once per phone); later starts keep the push token current silently.
 */
import { useEffect, useState, type ReactNode } from "react";
import { View } from "react-native";
import { NotificationPrompt } from "../screens/NotificationPrompt";
import { SignInScreen } from "../screens/SignInScreen";
import { colors } from "../theme/tokens";
import { useAuthState, useServices } from "./servicesContext";

export function AuthGate({ children }: { children: ReactNode }) {
  const { auth, push } = useServices();
  const state = useAuthState(auth);
  // null: not decided yet for this session.
  const [askPush, setAskPush] = useState<boolean | null>(null);

  useEffect(() => {
    if (state.status !== "signedIn") {
      setAskPush(null);
      return;
    }
    if (!push) {
      setAskPush(false);
      return;
    }
    let live = true;
    if (state.firstSignIn) {
      void push
        .shouldAsk()
        .catch(() => false)
        .then((ask) => {
          if (live) setAskPush(ask);
        });
    } else {
      setAskPush(false);
      void push.sync().catch(() => false);
    }
    return () => {
      live = false;
    };
  }, [state, push]);

  if (state.status === "demo") return <>{children}</>;
  if (state.status === "loading") return <View style={{ flex: 1, backgroundColor: colors.bg }} />;
  if (state.status === "signedOut") return <SignInScreen reason={state.reason} />;
  if (askPush === null && state.firstSignIn) return <View style={{ flex: 1, backgroundColor: colors.bg }} />;
  if (askPush && push) return <NotificationPrompt push={push} onDone={() => setAskPush(false)} />;
  return <>{children}</>;
}
