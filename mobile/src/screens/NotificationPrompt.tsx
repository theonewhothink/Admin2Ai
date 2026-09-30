/**
 * Asked once, right after the first sign-in: one line on why, then the OS
 * prompt only if the owner taps "Turn on" (§42: notifications are rare).
 */
import { useState } from "react";
import { StyleSheet, View } from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import { copy } from "../copy";
import type { PushRegistration } from "../notifications/push";
import { colors, space } from "../theme/tokens";
import { Icon } from "../ui/Icon";
import { Button, FadeIn, T } from "../ui/primitives";

export function NotificationPrompt({ push, onDone }: { push: PushRegistration; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  return (
    <SafeAreaView style={styles.screen}>
      <FadeIn>
        <View style={styles.center}>
          <View style={styles.icon}>
            <Icon name="bell" size={30} color={colors.text} />
          </View>
          <T variant="title" style={{ textAlign: "center" }}>
            {copy.push.title}
          </T>
          <T variant="small" style={{ textAlign: "center" }}>
            {copy.push.body}
          </T>
          <View style={styles.actions}>
            <Button
              label={copy.push.enable}
              busy={busy}
              onPress={() => {
                setBusy(true);
                void push.enable().finally(onDone);
              }}
            />
            <Button
              label={copy.push.later}
              kind="quiet"
              disabled={busy}
              onPress={() => {
                void push.decline().finally(onDone);
              }}
            />
          </View>
        </View>
      </FadeIn>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg, justifyContent: "center" },
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
  actions: { alignSelf: "stretch", gap: space.s1, marginTop: space.s2 },
});
