/**
 * Connections (opened from a "needs reconnecting" notification, §47-48):
 * which sources sync, which need the owner. Reconnecting signs in to Google,
 * Microsoft or the bank, which happens on the web; the phone opens it there.
 */
import { useRouter } from "expo-router";
import { ActivityIndicator, Linking, Pressable, StyleSheet, View } from "react-native";
import type { Connection } from "../api/types";
import { useLoaded } from "../app/hooks";
import { useServices } from "../app/servicesContext";
import { WEB_URL } from "../config";
import { copy } from "../copy";
import { sourceNote } from "../models/source";
import { colors, space, toneColors } from "../theme/tokens";
import { Icon, type IconName } from "../ui/Icon";
import { Button, Card, Dot, Note, Screen, T } from "../ui/primitives";

const KIND_ICON: Record<Connection["kind"], IconName> = { email: "mail", bank: "bank", accountant: "user" };

export function ConnectionsScreen() {
  const { api } = useServices();
  const router = useRouter();
  const { loaded, refreshing, refresh } = useLoaded(() => api.getHome());

  const back = (
    <Pressable accessibilityRole="button" accessibilityLabel={copy.account.back} onPress={() => router.back()} style={styles.back} hitSlop={8}>
      <Icon name="chevronLeft" size={20} color={colors.text2} />
      <T variant="small">{copy.account.back}</T>
    </Pressable>
  );

  if (!loaded) {
    return (
      <Screen>
        {back}
        <ActivityIndicator color={colors.text2} style={{ marginTop: space.s8 }} />
      </Screen>
    );
  }

  const connections = loaded.data.connections;
  const stale = connections.some((c) => c.status === "stale");
  return (
    <Screen refreshing={refreshing} onRefresh={() => void refresh()}>
      {back}
      <T variant="title" style={{ marginBottom: space.s2 }}>
        {copy.connections.title}
      </T>
      <Note text={sourceNote(loaded, new Date())} />
      {connections.length === 0 ? <T variant="small">{copy.connections.empty}</T> : null}
      <View style={{ gap: space.s1 + 4 }}>
        {connections.map((c) => {
          const tone = c.status === "healthy" ? "good" : "attention";
          return (
            <Card key={c.id}>
              <View style={styles.row} accessible accessibilityLabel={`${c.name}, ${c.account}, ${c.status === "healthy" ? copy.connections.healthy : copy.connections.stale}`}>
                <Icon name={KIND_ICON[c.kind]} size={20} color={colors.text2} />
                <View style={{ flex: 1, gap: 2 }}>
                  <T variant="bodyStrong">{c.name}</T>
                  <T variant="small" numberOfLines={1}>
                    {c.account}
                  </T>
                </View>
                <View style={styles.status}>
                  <Dot tone={tone} />
                  <T variant="meta" style={{ color: toneColors(tone).fg }}>
                    {c.status === "healthy" ? copy.connections.healthy : copy.connections.stale}
                  </T>
                </View>
              </View>
            </Card>
          );
        })}
      </View>
      {stale && WEB_URL ? (
        <View style={{ marginTop: space.s3, gap: space.s1 }}>
          <T variant="small">{copy.connections.reconnectHint}</T>
          <Button label={copy.connections.reconnect} onPress={() => void Linking.openURL(`${WEB_URL}/`)} />
        </View>
      ) : null}
    </Screen>
  );
}

const styles = StyleSheet.create({
  back: { flexDirection: "row", alignItems: "center", gap: space.s0, alignSelf: "flex-start", marginBottom: space.s2 },
  row: { flexDirection: "row", alignItems: "center", gap: space.s2 - 4 },
  status: { flexDirection: "row", alignItems: "center", gap: space.s0 + 2 },
});
