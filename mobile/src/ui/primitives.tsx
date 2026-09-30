/**
 * Small building blocks for every screen. Styling comes only from the tokens,
 * so the phone and the web read as one product (§30-33).
 */
import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ActivityIndicator,
  Animated,
  Easing,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
  type StyleProp,
  type TextStyle,
  type ViewStyle,
} from "react-native";
import { SafeAreaView } from "react-native-safe-area-context";
import type { Tone } from "../api/types";
import { colors, motion, radius, shadow, space, toneColors, type } from "../theme/tokens";
import { Icon } from "./Icon";

const ease = Easing.bezier(...motion.easing);

type Variant = keyof typeof type;

export function T({
  variant = "body",
  style,
  children,
  numberOfLines,
}: {
  variant?: Variant;
  style?: StyleProp<TextStyle>;
  children: ReactNode;
  numberOfLines?: number;
}) {
  return (
    <Text style={[type[variant], style]} numberOfLines={numberOfLines} maxFontSizeMultiplier={1.6}>
      {children}
    </Text>
  );
}

export function Screen({
  title,
  children,
  refreshing,
  onRefresh,
  scroll = true,
}: {
  title?: string;
  children: ReactNode;
  refreshing?: boolean;
  onRefresh?: () => void;
  scroll?: boolean;
}) {
  const body = (
    <>
      {title ? (
        <T variant="title" style={styles.screenTitle}>
          {title}
        </T>
      ) : null}
      {children}
    </>
  );
  return (
    <SafeAreaView style={styles.screen} edges={["top", "left", "right"]}>
      {scroll ? (
        <ScrollView
          contentContainerStyle={styles.scroll}
          keyboardShouldPersistTaps="handled"
          refreshControl={
            onRefresh ? <RefreshControl refreshing={Boolean(refreshing)} onRefresh={onRefresh} tintColor={colors.text2} /> : undefined
          }
        >
          {body}
        </ScrollView>
      ) : (
        <View style={styles.scroll}>{body}</View>
      )}
    </SafeAreaView>
  );
}

export function Card({ children, style }: { children: ReactNode; style?: StyleProp<ViewStyle> }) {
  return <View style={[styles.card, style]}>{children}</View>;
}

export function Dot({ tone, size = 8 }: { tone: Tone; size?: number }) {
  return <View style={{ width: size, height: size, borderRadius: size / 2, backgroundColor: toneColors(tone).dot }} />;
}

export function Button({
  label,
  onPress,
  kind = "primary",
  disabled,
  busy,
  accessibilityHint,
}: {
  label: string;
  onPress: () => void;
  kind?: "primary" | "secondary" | "quiet";
  disabled?: boolean;
  busy?: boolean;
  accessibilityHint?: string;
}) {
  const inactive = Boolean(disabled || busy);
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityHint={accessibilityHint}
      accessibilityState={{ disabled: inactive, busy: Boolean(busy) }}
      disabled={inactive}
      onPress={onPress}
      style={({ pressed }) => [
        styles.button,
        kind === "primary" && { backgroundColor: pressed ? colors.inkPressed : colors.ink },
        kind === "secondary" && [styles.secondary, pressed && { backgroundColor: colors.surfaceHover }],
        kind === "quiet" && styles.quiet,
        inactive && { opacity: 0.45 },
      ]}
    >
      {busy ? (
        <ActivityIndicator color={kind === "primary" ? colors.onInk : colors.text} />
      ) : (
        <T variant="bodyStrong" style={{ color: kind === "primary" ? colors.onInk : colors.text, textAlign: "center" }}>
          {label}
        </T>
      )}
    </Pressable>
  );
}

export function Checkbox({ checked, onChange, label }: { checked: boolean; onChange: (next: boolean) => void; label: string }) {
  return (
    <Pressable
      accessibilityRole="checkbox"
      accessibilityState={{ checked }}
      accessibilityLabel={label}
      onPress={() => onChange(!checked)}
      style={styles.checkRow}
      hitSlop={8}
    >
      <View style={[styles.checkBox, checked && { backgroundColor: colors.ink, borderColor: colors.ink }]}>
        {checked ? <Icon name="check" size={16} color={colors.onInk} strokeWidth={2.2} /> : null}
      </View>
      <T variant="small" style={{ flex: 1, color: colors.text }}>
        {label}
      </T>
    </Pressable>
  );
}

export function Chip({ label, onPress }: { label: string; onPress?: () => void }) {
  return (
    <Pressable
      accessibilityRole={onPress ? "button" : "text"}
      disabled={!onPress}
      onPress={onPress}
      style={({ pressed }) => [styles.chip, pressed && { backgroundColor: colors.surfaceHover }]}
    >
      <T variant="meta" style={{ color: colors.text }}>
        {label}
      </T>
    </Pressable>
  );
}

/** A tinted strip for plain status messages. Red only for real risk. */
export function Banner({ text, tone = "neutral" }: { text: string; tone?: Tone }) {
  const c = toneColors(tone);
  return (
    <View style={[styles.banner, { backgroundColor: c.soft }]} accessibilityRole="alert">
      <Dot tone={tone} />
      <T variant="small" style={{ flex: 1, color: tone === "neutral" ? colors.text2 : c.fg }}>
        {text}
      </T>
    </View>
  );
}

/** "Why am I seeing this?" with the reasons behind a decision (§54-55). */
export function Disclosure({ summary, children }: { summary: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <View>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ expanded: open }}
        onPress={() => setOpen((v) => !v)}
        style={styles.disclosure}
        hitSlop={6}
      >
        <T variant="meta">{summary}</T>
        <View style={{ transform: [{ rotate: open ? "180deg" : "0deg" }] }}>
          <Icon name="chevronDown" size={16} color={colors.text2} />
        </View>
      </Pressable>
      {open ? <FadeIn>{children}</FadeIn> : null}
    </View>
  );
}

export function Bullets({ items }: { items: readonly string[] }) {
  return (
    <View style={{ gap: space.s1, marginTop: space.s1 }}>
      {items.map((item) => (
        <View key={item} style={{ flexDirection: "row", gap: space.s1 }}>
          <T variant="small">•</T>
          <T variant="small" style={{ flex: 1 }}>
            {item}
          </T>
        </View>
      ))}
    </View>
  );
}

/** Fades content in over 180ms: used only when state changes. */
export function FadeIn({ children, duration = motion.base }: { children: ReactNode; duration?: number }) {
  const opacity = useRef(new Animated.Value(0)).current;
  useEffect(() => {
    Animated.timing(opacity, { toValue: 1, duration, easing: ease, useNativeDriver: true }).start();
  }, [opacity, duration]);
  return <Animated.View style={{ opacity }}>{children}</Animated.View>;
}

export function Note({ text }: { text: string | null }) {
  return text ? (
    <T variant="meta" style={{ marginBottom: space.s2 }}>
      {text}
    </T>
  ) : null;
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: colors.bg },
  scroll: { paddingHorizontal: space.s2, paddingTop: space.s2, paddingBottom: 120 },
  screenTitle: { marginBottom: space.s2 },
  card: {
    backgroundColor: colors.surface,
    borderRadius: radius.card,
    padding: space.s2,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line,
    ...shadow,
  },
  button: {
    minHeight: 48,
    borderRadius: radius.control,
    paddingHorizontal: space.s2,
    alignItems: "center",
    justifyContent: "center",
  },
  secondary: { backgroundColor: colors.surface, borderWidth: 1, borderColor: colors.line2 },
  quiet: { backgroundColor: "transparent", minHeight: 40 },
  checkRow: { flexDirection: "row", alignItems: "center", gap: space.s1 + 4, paddingVertical: space.s1 },
  checkBox: {
    width: 22,
    height: 22,
    borderRadius: 6,
    borderWidth: 1.5,
    borderColor: colors.line2,
    alignItems: "center",
    justifyContent: "center",
    backgroundColor: colors.surface,
  },
  chip: {
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: colors.line2,
    paddingHorizontal: space.s2 - 4,
    paddingVertical: space.s0 + 2,
    backgroundColor: colors.surface,
  },
  banner: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.s1,
    borderRadius: radius.control,
    padding: space.s2 - 4,
    marginBottom: space.s2,
  },
  disclosure: { flexDirection: "row", alignItems: "center", gap: space.s0, paddingVertical: space.s1 },
});
