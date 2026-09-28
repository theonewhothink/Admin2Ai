/**
 * Bottom tabs (§40): Home, Needs You, Scan (large, centre), Activity, Ask.
 * The Scan button is ink, not colour: emerald is kept for "all good".
 */
import type { ComponentProps } from "react";
import { Pressable, StyleSheet, View } from "react-native";
import type { Tabs } from "expo-router";
import { useNeeds } from "../app/needs";
import { colors, fonts, space } from "../theme/tokens";
import { Icon } from "../ui/Icon";
import { T } from "../ui/primitives";
import { badgeText, tabFor } from "./tabs";

type TabBarProps = Parameters<NonNullable<ComponentProps<typeof Tabs>["tabBar"]>>[0];

export function TabBar({ state, navigation, insets }: TabBarProps) {
  const { items, loaded } = useNeeds();
  const badge = loaded ? badgeText(items.length) : null;

  return (
    <View style={[styles.bar, { paddingBottom: Math.max(insets.bottom, space.s1) }]} accessibilityRole="tablist">
      {state.routes.map((route, index) => {
        const tab = tabFor(route.name);
        if (!tab) return null;
        const focused = state.index === index;
        const onPress = () => {
          const event = navigation.emit({ type: "tabPress", target: route.key, canPreventDefault: true });
          if (!focused && !event.defaultPrevented) navigation.navigate(route.name, route.params);
        };
        const color = focused ? colors.text : colors.text3;
        const showBadge = tab.route === "needs-you" && badge !== null;

        if (tab.center) {
          return (
            <Pressable
              key={route.key}
              onPress={onPress}
              accessibilityRole="tab"
              accessibilityState={{ selected: focused }}
              accessibilityLabel={tab.label}
              style={styles.centerSlot}
            >
              <View style={[styles.centerButton, focused && styles.centerFocused]}>
                <Icon name={tab.icon} size={28} color={colors.onInk} strokeWidth={1.9} />
              </View>
              <T variant="meta" style={[styles.label, { color }]}>
                {tab.label}
              </T>
            </Pressable>
          );
        }
        return (
          <Pressable
            key={route.key}
            onPress={onPress}
            accessibilityRole="tab"
            accessibilityState={{ selected: focused }}
            accessibilityLabel={showBadge ? `${tab.label}, ${items.length}` : tab.label}
            style={styles.slot}
          >
            <View>
              <Icon name={tab.icon} size={24} color={color} strokeWidth={focused ? 1.9 : 1.6} />
              {showBadge ? (
                <View style={styles.badge}>
                  <T variant="meta" style={styles.badgeText}>
                    {badge}
                  </T>
                </View>
              ) : null}
            </View>
            <T variant="meta" style={[styles.label, { color }]} numberOfLines={1}>
              {tab.label}
            </T>
          </Pressable>
        );
      })}
    </View>
  );
}

const styles = StyleSheet.create({
  bar: {
    flexDirection: "row",
    alignItems: "flex-end",
    backgroundColor: colors.surface,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.line2,
    paddingTop: space.s1,
  },
  slot: { flex: 1, alignItems: "center", gap: 2, paddingVertical: space.s0, minHeight: 48 },
  centerSlot: { flex: 1, alignItems: "center", gap: 2 },
  centerButton: {
    width: 60,
    height: 60,
    borderRadius: 30,
    marginTop: -26,
    backgroundColor: colors.ink,
    alignItems: "center",
    justifyContent: "center",
    borderWidth: 4,
    borderColor: colors.bg,
  },
  centerFocused: { backgroundColor: colors.inkPressed },
  label: { fontSize: 12, lineHeight: 16, fontFamily: fonts.medium },
  badge: {
    position: "absolute",
    top: -4,
    right: -10,
    minWidth: 18,
    height: 18,
    borderRadius: 9,
    paddingHorizontal: 4,
    backgroundColor: colors.attentionDot,
    alignItems: "center",
    justifyContent: "center",
  },
  badgeText: { color: colors.text, fontSize: 11, lineHeight: 14, fontFamily: fonts.semibold },
});
