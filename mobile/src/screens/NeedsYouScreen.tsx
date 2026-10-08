/** Needs You: only real exceptions, as decision cards (§34-41). */
import { ActivityIndicator, View } from "react-native";
import { useNeeds } from "../app/needs";
import { copy } from "../copy";
import { sourceNote } from "../models/source";
import { colors, space } from "../theme/tokens";
import { Icon } from "../ui/Icon";
import { Card, FadeIn, Note, Screen, T } from "../ui/primitives";
import { DecisionCard } from "./DecisionCard";

export function NeedsYouScreen() {
  const { loaded, items, refreshing, refresh, resolve } = useNeeds();
  return (
    <Screen title={copy.needs.title} refreshing={refreshing} onRefresh={() => void refresh()}>
      {!loaded ? (
        <ActivityIndicator color={colors.text2} style={{ marginTop: space.s8 }} />
      ) : (
        <>
          <Note text={sourceNote(loaded, new Date())} />
          {items.length === 0 ? (
            <FadeIn>
              <Card style={{ flexDirection: "row", alignItems: "center", gap: space.s1 + 4 }}>
                <Icon name="check" size={22} color={colors.good} strokeWidth={2} />
                <T variant="body" style={{ flex: 1 }}>
                  {copy.needs.empty}
                </T>
              </Card>
            </FadeIn>
          ) : (
            <View style={{ gap: space.s2 }}>
              {items.map((item) => (
                <DecisionCard key={item.id} item={item} onResolved={() => resolve(item.id)} />
              ))}
            </View>
          )}
        </>
      )}
    </Screen>
  );
}
