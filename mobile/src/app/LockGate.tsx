/**
 * Biometric lock on app open (§52). The app stays mounted underneath so
 * navigation survives a re-lock, but it is hidden from sight and from screen
 * readers until unlocked. A plain cover also hides business data in the app
 * switcher.
 */
import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { AppState, StyleSheet, View, type AppStateStatus } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { copy } from "../copy";
import type { LockState } from "../security/lock";
import { colors, space } from "../theme/tokens";
import { Icon } from "../ui/Icon";
import { Button, FadeIn, T } from "../ui/primitives";
import { useServices } from "./servicesContext";

const UnlockedContext = createContext(false);

/** True once the owner has unlocked (or the phone has no passcode and they continued). */
export function useUnlocked(): boolean {
  return useContext(UnlockedContext);
}

export function LockGate({ children }: { children: ReactNode }) {
  const { lock } = useServices();
  const [state, setState] = useState<LockState>(lock.current);
  const [appState, setAppState] = useState<AppStateStatus>(AppState.currentState);
  const [acknowledged, setAcknowledged] = useState(false);

  useEffect(() => lock.subscribe(setState), [lock]);

  useEffect(() => {
    void lock.unlock();
    let last: AppStateStatus = AppState.currentState;
    const sub = AppState.addEventListener("change", (next) => {
      if (next === "background") lock.onBackground();
      // Only prompt when coming back from the background: returning from our own
      // Face ID sheet ("inactive" -> "active") after a cancel must not loop.
      if (next === "active" && last === "background") {
        lock.onForeground();
        if (lock.current.status === "locked") void lock.unlock();
      }
      last = next;
      setAppState(next);
    });
    return () => sub.remove();
  }, [lock]);

  const open = state.status === "unlocked" || (state.status === "unprotected" && acknowledged);
  return (
    <UnlockedContext.Provider value={open}>
      <View style={{ flex: 1 }}>
        <View
          style={{ flex: 1 }}
          importantForAccessibility={open ? "auto" : "no-hide-descendants"}
          accessibilityElementsHidden={!open}
        >
          {children}
        </View>
        {!open ? (
          <LockScreen state={state} onUnlock={() => void lock.unlock()} onContinue={() => setAcknowledged(true)} />
        ) : appState !== "active" ? (
          <View style={[StyleSheet.absoluteFill, styles.cover]} />
        ) : null}
      </View>
    </UnlockedContext.Provider>
  );
}

function LockScreen({ state, onUnlock, onContinue }: { state: LockState; onUnlock: () => void; onContinue: () => void }) {
  const unprotected = state.status === "unprotected";
  return (
    <SafeAreaView style={[StyleSheet.absoluteFill, styles.cover]}>
      <FadeIn>
        <View style={styles.center}>
          <View style={styles.icon}>
            <Icon name="lock" size={30} color={colors.text} />
          </View>
          <T variant="title" style={{ textAlign: "center" }}>
            {copy.lock.title}
          </T>
          <T variant="small" style={{ textAlign: "center" }}>
            {unprotected ? copy.lock.noPasscode : state.status === "locked" && state.message ? state.message : copy.lock.body}
          </T>
          <View style={{ alignSelf: "stretch", marginTop: space.s2 }}>
            {unprotected ? (
              <Button label={copy.lock.continue} onPress={onContinue} />
            ) : (
              <Button label={copy.lock.unlock} busy={state.status === "unlocking"} onPress={onUnlock} />
            )}
          </View>
        </View>
      </FadeIn>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  cover: { backgroundColor: colors.bg, justifyContent: "center" },
  center: { alignItems: "center", gap: space.s1 + 4, paddingHorizontal: space.s4 },
  icon: {
    width: 64,
    height: 64,
    borderRadius: 32,
    backgroundColor: colors.surface,
    alignItems: "center",
    justifyContent: "center",
    marginBottom: space.s1,
  },
});
