/**
 * Receives shares from other apps (§12) and hands them to the offline queue.
 * Waits until the app is unlocked, so the confirmation is actually seen.
 */
import { useShareIntentContext } from "expo-share-intent";
import { useEffect, useRef, useState } from "react";
import { StyleSheet, View } from "react-native";
import { discardTemporary, readFileBytes } from "../offline/expo/files";
import { DEFAULT_PIPELINE_CONFIG } from "../offline/pipeline";
import { fromShareIntent, ingestShare, shareMessage } from "../share/ingest";
import { routeShare } from "../share/route";
import { colors, space } from "../theme/tokens";
import { Icon } from "../ui/Icon";
import { Card, FadeIn, T } from "../ui/primitives";
import { useUnlocked } from "./LockGate";
import { useServices } from "./servicesContext";

const TOAST_MS = 3_500;

export function ShareHandler() {
  const { hasShareIntent, shareIntent, resetShareIntent } = useShareIntentContext();
  const unlocked = useUnlocked();
  const { offline } = useServices();
  const [message, setMessage] = useState<string | null>(null);
  const busy = useRef(false);

  useEffect(() => {
    if (!hasShareIntent || !unlocked || busy.current) return;
    busy.current = true;
    void (async () => {
      try {
        const route = routeShare(fromShareIntent(shareIntent), DEFAULT_PIPELINE_CONFIG.maxBytes);
        const result = await ingestShare(route, {
          sink: offline.pipeline,
          readFile: readFileBytes,
          discard: discardTemporary,
          now: () => new Date(),
        });
        setMessage(shareMessage(result));
        void offline.runner.kick();
      } finally {
        resetShareIntent();
        busy.current = false;
      }
    })();
  }, [hasShareIntent, shareIntent, unlocked, offline, resetShareIntent]);

  useEffect(() => {
    if (!message) return;
    const t = setTimeout(() => setMessage(null), TOAST_MS);
    return () => clearTimeout(t);
  }, [message]);

  if (!message) return null;
  return (
    <View pointerEvents="none" style={styles.toastWrap} accessibilityLiveRegion="polite">
      <FadeIn key={message}>
        <Card style={styles.toast}>
          <Icon name="inboxIn" size={20} color={colors.text} />
          <T variant="bodyStrong" style={{ flex: 1 }}>
            {message}
          </T>
        </Card>
      </FadeIn>
    </View>
  );
}

const styles = StyleSheet.create({
  toastWrap: { position: "absolute", left: space.s2, right: space.s2, bottom: 110 },
  toast: { flexDirection: "row", alignItems: "center", gap: space.s1 + 2 },
});
