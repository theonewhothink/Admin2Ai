/**
 * Home (§41): "Everything is under control." or "I need 2 things from you.",
 * then Needs you · September 94% · Handled today, then each company.
 */
import { useRouter } from "expo-router";
import { ActivityIndicator, Pressable, StyleSheet, View } from "react-native";
import type { CompanySummary } from "../api/types";
import { useLoaded } from "../app/hooks";
import { useNeeds } from "../app/needs";
import { useServices } from "../app/servicesContext";
import { buildHomeView, type Tile } from "../models/home";
import { REQUIRES_SIGN_IN } from "../config";
import { copy } from "../copy";
import { sourceNote } from "../models/source";
import { colors, space, toneColors } from "../theme/tokens";
import { Icon } from "../ui/Icon";
import { Banner, Card, Dot, FadeIn, Note, Screen, T } from "../ui/primitives";

export function HomeScreen() {
  const { api } = useServices();
  const needs = useNeeds();
  const router = useRouter();
  const { loaded, refreshing, refresh } = useLoaded(() => api.getHome());

  if (!loaded) {
    return (
      <Screen>
        <ActivityIndicator color={colors.text2} style={{ marginTop: space.s8 }} />
      </Screen>
    );
  }

  const now = new Date();
  // The live Needs You list is the source of truth once loaded; it reflects answers at once.
  const needsCount = needs.loaded ? needs.items.length : loaded.data.needsYouCount;
  const view = buildHomeView(loaded.data, needsCount, now);

  return (
    <Screen
      refreshing={refreshing || needs.refreshing}
      onRefresh={() => {
        void refresh();
        void needs.refresh();
      }}
    >
      <View style={styles.topRow}>
        <T variant="meta">{view.greeting}</T>
        {REQUIRES_SIGN_IN ? (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={copy.account.title}
            onPress={() => router.push("/account")}
            style={styles.account}
            hitSlop={8}
          >
            <Icon name="user" size={20} color={colors.text2} />
          </Pressable>
        ) : null}
      </View>
      <FadeIn key={view.status.text}>
        <View style={styles.statusRow} accessibilityRole="header">
          <View style={{ marginTop: 13 }}>
            <Dot tone={view.status.tone} size={10} />
          </View>
          <T variant="display" style={{ flex: 1 }}>
            {view.status.text}
          </T>
        </View>
      </FadeIn>
      <Note text={sourceNote(loaded, now)} />
      {view.banner ? <Banner text={view.banner.text} tone={view.banner.tone} /> : null}

      <View style={styles.tiles}>
        {view.tiles.map((tile) => (
          <TileCard key={tile.id} tile={tile} onPress={tile.id === "needs" ? () => router.navigate("/needs-you") : undefined} />
        ))}
      </View>

      <T variant="heading" style={styles.section}>
        Your businesses
      </T>
      <View style={{ gap: space.s1 + 4 }}>
        {view.companies.map((c) => (
          <CompanyCard key={c.id} company={c} />
        ))}
      </View>
    </Screen>
  );
}

function TileCard({ tile, onPress }: { tile: Tile; onPress?: (() => void) | undefined }) {
  const tone = toneColors(tile.tone);
  const body = (
    <Card style={styles.tile}>
      <T variant="number" style={{ color: tile.tone === "good" ? tone.fg : colors.text }}>
        {tile.value}
      </T>
      <View style={styles.tileLabel}>
        {tile.tone !== "neutral" ? <Dot tone={tile.tone} size={6} /> : null}
        <T variant="meta" numberOfLines={1}>
          {tile.label}
        </T>
      </View>
    </Card>
  );
  return onPress ? (
    <Pressable style={{ flex: 1 }} onPress={onPress} accessibilityRole="button" accessibilityLabel={`${tile.label} ${tile.value}`}>
      {body}
    </Pressable>
  ) : (
    <View style={{ flex: 1 }} accessible accessibilityLabel={`${tile.label} ${tile.value}`}>
      {body}
    </View>
  );
}

function CompanyCard({ company }: { company: CompanySummary }) {
  const tone = toneColors(company.tone);
  return (
    <Card>
      <View style={styles.companyRow}>
        <View style={{ flex: 1, gap: 2 }}>
          <T variant="bodyStrong">{company.name}</T>
          {company.detail ? <T variant="small">{company.detail}</T> : null}
        </View>
        <View style={styles.companyStatus}>
          <Dot tone={company.tone} />
          <T variant="meta" style={{ color: company.tone === "neutral" ? colors.text2 : tone.fg }}>
            {company.statusLabel}
          </T>
        </View>
      </View>
    </Card>
  );
}

const styles = StyleSheet.create({
  topRow: { flexDirection: "row", alignItems: "center", justifyContent: "space-between", minHeight: 36, marginBottom: space.s1 },
  account: {
    width: 36,
    height: 36,
    borderRadius: 18,
    backgroundColor: colors.surface,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.line2,
    alignItems: "center",
    justifyContent: "center",
  },
  statusRow: { flexDirection: "row", gap: space.s1 + 2, marginBottom: space.s1 },
  tiles: { flexDirection: "row", gap: space.s1, marginTop: space.s1 },
  tile: { paddingVertical: space.s2, gap: space.s0 },
  tileLabel: { flexDirection: "row", alignItems: "center", gap: space.s0 + 2 },
  section: { marginTop: space.s4, marginBottom: space.s1 + 4 },
  companyRow: { flexDirection: "row", alignItems: "center", gap: space.s1 + 4 },
  companyStatus: { flexDirection: "row", alignItems: "center", gap: space.s0 + 2 },
});
