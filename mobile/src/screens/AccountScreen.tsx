/**
 * Account (from Home): who is signed in, notifications, sign out.
 * Sign-out stops this phone's notifications, ends the session and deletes the
 * token; documents still waiting to send stay on the phone (they are sealed)
 * and go after the next sign-in.
 */
import { useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { Alert, Linking, Pressable, StyleSheet, View } from "react-native";
import type { Me } from "../api/client";
import { useQueueSummary } from "../app/hooks";
import { useServices } from "../app/servicesContext";
import { copy } from "../copy";
import type { PermissionStatus } from "../notifications/push";
import { colors, space } from "../theme/tokens";
import { Icon } from "../ui/Icon";
import { Banner, Button, Card, Screen, T } from "../ui/primitives";

export function AccountScreen() {
  const { api, auth, push } = useServices();
  const router = useRouter();
  const queue = useQueueSummary();
  const [me, setMe] = useState<Me | null>(null);
  const [notifications, setNotifications] = useState<PermissionStatus | "unavailable" | null>(null);
  const [leaving, setLeaving] = useState(false);

  useEffect(() => {
    let live = true;
    void api.getMe().then((m) => {
      if (live) setMe(m);
    });
    if (push) {
      void push
        .sync()
        .then((on) => (on ? "granted" : push.shouldAsk().then((ask) => (ask ? "undetermined" : "denied"))))
        .catch(() => "denied" as const)
        .then((s) => {
          if (live) setNotifications(s);
        });
    }
    return () => {
      live = false;
    };
  }, [api, push]);

  const turnOn = async () => {
    if (!push) return;
    const result = await push.enable();
    setNotifications(result === "on" ? "granted" : result === "unavailable" ? "unavailable" : "denied");
  };

  const signOut = () => {
    const waiting = queue ? queue.waiting + queue.held.length : 0;
    Alert.alert(copy.account.signOutConfirm, waiting > 0 ? copy.account.signOutWaiting(waiting) : undefined, [
      { text: copy.account.cancel, style: "cancel" },
      {
        text: copy.account.signOut,
        style: "destructive",
        onPress: () => {
          setLeaving(true);
          void auth.signOut();
        },
      },
    ]);
  };

  return (
    <Screen>
      <Pressable accessibilityRole="button" accessibilityLabel={copy.account.back} onPress={() => router.back()} style={styles.back} hitSlop={8}>
        <Icon name="chevronLeft" size={20} color={colors.text2} />
        <T variant="small">{copy.account.back}</T>
      </Pressable>
      <T variant="title" style={{ marginBottom: space.s2 }}>
        {copy.account.title}
      </T>

      {api.isDemo ? (
        <Banner text={copy.account.demo} />
      ) : (
        <View style={{ gap: space.s2 }}>
          <Card>
            <View style={styles.row}>
              <Icon name="user" size={20} color={colors.text2} />
              <View style={{ flex: 1, gap: 2 }}>
                <T variant="meta">{copy.account.signedInAs}</T>
                <T variant="bodyStrong">{me ? me.user.name || me.user.email : "…"}</T>
                {me && me.user.name ? <T variant="small">{me.user.email}</T> : null}
              </View>
            </View>
          </Card>

          {push ? (
            <Card>
              <View style={styles.row}>
                <Icon name="bell" size={20} color={colors.text2} />
                <View style={{ flex: 1, gap: 2 }}>
                  <T variant="bodyStrong">{copy.account.notifications}</T>
                  <T variant="small">
                    {notifications === "granted"
                      ? copy.account.notificationsOn
                      : notifications === "denied"
                        ? copy.push.denied
                        : notifications === null
                          ? "…"
                          : copy.account.notificationsOff}
                  </T>
                </View>
              </View>
              {notifications === "undetermined" || notifications === "unavailable" ? (
                <View style={{ marginTop: space.s2 }}>
                  <Button label={copy.account.turnOn} kind="secondary" onPress={() => void turnOn()} />
                </View>
              ) : notifications === "denied" ? (
                <View style={{ marginTop: space.s2 }}>
                  <Button label={copy.account.openSettings} kind="secondary" onPress={() => void Linking.openSettings()} />
                </View>
              ) : null}
            </Card>
          ) : null}

          <View style={{ marginTop: space.s2 }}>
            <Button label={copy.account.signOut} kind="secondary" busy={leaving} onPress={signOut} />
          </View>
        </View>
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  back: { flexDirection: "row", alignItems: "center", gap: space.s0, alignSelf: "flex-start", marginBottom: space.s2 },
  row: { flexDirection: "row", alignItems: "center", gap: space.s2 - 4 },
});
