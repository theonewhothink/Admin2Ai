/** Activity: a quiet record of what was handled (§42: quiet success). */
import { ActivityIndicator, StyleSheet, View } from "react-native";
import { SAMPLE_TODAY } from "../api/sample";
import { useLoaded } from "../app/hooks";
import { useServices } from "../app/servicesContext";
import { copy } from "../copy";
import { formatTime, localDayKey } from "../lib/dates";
import { formatMoney } from "../lib/money";
import { activityTone, groupActivity } from "../models/activity";
import { sourceNote } from "../models/source";
import { colors, space } from "../theme/tokens";
import { Card, Dot, Note, Screen, T } from "../ui/primitives";

export function ActivityScreen() {
  const { api } = useServices();
  const { loaded, refreshing, refresh } = useLoaded(() => api.getActivity());
  const now = new Date();
  const today = loaded?.data.today ?? (loaded?.source === "sample" ? SAMPLE_TODAY : localDayKey(now));
  const groups = loaded ? groupActivity(loaded.data.items, today) : [];

  return (
    <Screen title={copy.activity.title} refreshing={refreshing} onRefresh={() => void refresh()}>
      {!loaded ? (
        <ActivityIndicator color={colors.text2} style={{ marginTop: space.s8 }} />
      ) : (
        <>
          <Note text={sourceNote(loaded, now)} />
          {groups.length === 0 ? <T variant="small">{copy.activity.empty}</T> : null}
          {groups.map((group) => (
            <View key={group.day} style={{ marginBottom: space.s3 }}>
              <T variant="meta" style={{ marginBottom: space.s1 }}>
                {group.label}
              </T>
              <Card style={{ paddingVertical: space.s1 }}>
                {group.items.map((item, i) => {
                  const meta = [
                    formatTime(item.at),
                    item.companyName,
                    item.amount ? formatMoney(item.amount, item.currency ?? "EUR") : null,
                  ]
                    .filter(Boolean)
                    .join(" · ");
                  return (
                    <View key={item.id} style={[styles.row, i > 0 && styles.divider]}>
                      <View style={{ marginTop: 8 }}>
                        <Dot tone={activityTone(item.kind)} size={7} />
                      </View>
                      <View style={{ flex: 1, gap: 2 }}>
                        <T variant="body">{item.text}</T>
                        <T variant="meta">{meta}</T>
                      </View>
                    </View>
                  );
                })}
              </Card>
            </View>
          ))}
        </>
      )}
    </Screen>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: "row", gap: space.s1 + 4, paddingVertical: space.s1 + 4 },
  divider: { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.line },
});
