/**
 * Sources (from Home's "What I read" row): everything I read, the one plain line each gave ("6 payments since
 * 1 September: all have their invoice."), the companies they feed, and "Something missing?". Adding a mailbox or a
 * bank signs in to Google, Microsoft or the bank, which happens on the web: the phone understands what was typed and
 * opens Sources there with it filled in, or Ask with the text when it is something else.
 */
import { useRouter } from "expo-router";
import { useState } from "react";
import { ActivityIndicator, Linking, Pressable, StyleSheet, TextInput, View } from "react-native";
import type { UnderstandOutcome } from "../api/client";
import { useLoaded } from "../app/hooks";
import { useServices } from "../app/servicesContext";
import { WEB_URL } from "../config";
import { copy } from "../copy";
import { sourceNote } from "../models/source";
import { buildSourcesView } from "../models/sources";
import { colors, radius, space, toneColors, type } from "../theme/tokens";
import { Icon, type IconName } from "../ui/Icon";
import { Banner, Button, Card, Dot, FadeIn, Note, Screen, T } from "../ui/primitives";

const GROUP_ICON: Record<string, IconName> = {
  email: "mail",
  banks: "bank",
  cards: "bank",
  files: "document",
  accounting: "document",
  portals: "link",
  accountant: "user",
};

export function SourcesScreen() {
  const { api } = useServices();
  const router = useRouter();
  const { loaded, refreshing, refresh } = useLoaded(() => api.getSources());
  const [text, setText] = useState("");
  const [asked, setAsked] = useState<{ text: string; outcome: UnderstandOutcome } | null>(null);
  const [busy, setBusy] = useState(false);

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

  const view = buildSourcesView(loaded.data);
  const understand = async () => {
    const t = text.trim();
    if (!t || busy) return;
    setBusy(true);
    const outcome = await api.understandSource(t);
    setBusy(false);
    setAsked({ text: t, outcome });
  };

  return (
    <Screen refreshing={refreshing} onRefresh={() => void refresh()}>
      {back}
      <T variant="title" style={{ marginBottom: space.s1 }}>
        {copy.sources.title}
      </T>
      <Note text={sourceNote(loaded, new Date())} />
      <T variant="body" style={{ marginBottom: space.s2 }}>
        {view.summary}
      </T>
      <Banner text={view.coverage.text} tone={view.coverage.tone} />

      <T variant="heading" style={styles.section}>
        {copy.sources.missing}
      </T>
      <View style={styles.box}>
        <TextInput
          value={text}
          onChangeText={setText}
          placeholder={copy.sources.missingPlaceholder}
          placeholderTextColor={colors.text3}
          style={styles.input}
          returnKeyType="send"
          autoCapitalize="none"
          autoCorrect={false}
          onSubmitEditing={() => void understand()}
          accessibilityLabel={copy.sources.missingHint}
          maxLength={500}
        />
      </View>
      <View style={{ marginTop: space.s1 }}>
        <Button label={copy.sources.understand} onPress={() => void understand()} disabled={!text.trim()} busy={busy} />
      </View>
      {asked ? <Understood asked={asked} onAsk={(q) => router.navigate({ pathname: "/ask", params: { q } })} /> : null}

      <T variant="heading" style={styles.section}>
        {copy.sources.read}
      </T>
      {view.sections.length === 0 ? <T variant="small">{copy.sources.nothingRead}</T> : null}
      <View style={{ gap: space.s2 }}>
        {view.sections.map((section) => (
          <View key={section.id} style={{ gap: space.s1 }}>
            <T variant="meta">{section.title}</T>
            {section.rows.map((row) => {
              const tone = row.status ? toneColors(row.status.tone) : null;
              return (
                <Card key={row.id}>
                  <View
                    style={styles.row}
                    accessible
                    accessibilityLabel={[row.name, row.where, row.line, row.status?.label].filter(Boolean).join(", ")}
                  >
                    <Icon name={GROUP_ICON[section.id] ?? "document"} size={20} color={colors.text2} />
                    <View style={{ flex: 1, gap: 2 }}>
                      <T variant="bodyStrong">{row.name}</T>
                      {row.where ? <T variant="meta">{row.where}</T> : null}
                      {row.line ? (
                        <T variant="small" style={{ color: colors.text, marginTop: 2 }}>
                          {row.line}
                        </T>
                      ) : null}
                      {row.status && tone ? (
                        <View style={styles.status}>
                          <Dot tone={row.status.tone} />
                          <T variant="meta" style={{ color: tone.fg }}>
                            {row.status.label}
                          </T>
                        </View>
                      ) : null}
                    </View>
                  </View>
                </Card>
              );
            })}
          </View>
        ))}
      </View>

      {view.companies.length ? (
        <>
          <T variant="heading" style={styles.section}>
            {copy.sources.companies}
          </T>
          <View style={{ gap: space.s1 + 4 }}>
            {view.companies.map((c) => (
              <Card key={c.id}>
                <View style={{ gap: 2 }} accessible accessibilityLabel={[c.name, c.tax, c.sources].filter(Boolean).join(", ")}>
                  <T variant="bodyStrong">{c.name}</T>
                  {c.tax ? <T variant="meta">{c.tax}</T> : null}
                  <T variant="small" style={{ marginTop: 2 }}>
                    {c.sources}
                  </T>
                </View>
              </Card>
            ))}
          </View>
        </>
      ) : null}
    </Screen>
  );
}

/** What "Something missing?" understood: finish on the web (signing in happens there), or ask about it. */
function Understood({ asked, onAsk }: { asked: { text: string; outcome: UnderstandOutcome }; onAsk: (q: string) => void }) {
  const { outcome } = asked;
  if (!outcome.ok) {
    return (
      <FadeIn>
        <T variant="small" style={{ marginTop: space.s2 }}>
          {outcome.reason === "demo" ? copy.sources.demo : copy.sources.offline}
        </T>
      </FadeIn>
    );
  }
  const u = outcome.understood;
  const web = WEB_URL && u.kind !== "ask" && !u.already ? `${WEB_URL}/sources?missing=${encodeURIComponent(asked.text)}` : null;
  return (
    <FadeIn key={asked.text}>
      <Card style={{ marginTop: space.s2, gap: space.s2 }}>
        <T variant="body">{u.message}</T>
        {u.kind === "ask" ? <Button label={copy.sources.askInstead} kind="secondary" onPress={() => onAsk(asked.text)} /> : null}
        {web ? (
          <Button
            label={copy.sources.finishOnWeb}
            kind="secondary"
            accessibilityHint={copy.sources.finishOnWebHint}
            onPress={() => void Linking.openURL(web)}
          />
        ) : null}
      </Card>
    </FadeIn>
  );
}

const styles = StyleSheet.create({
  back: { flexDirection: "row", alignItems: "center", gap: space.s0, alignSelf: "flex-start", marginBottom: space.s2 },
  section: { marginTop: space.s4, marginBottom: space.s1 + 4 },
  box: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: colors.surface,
    borderRadius: radius.card,
    borderWidth: 1,
    borderColor: colors.line2,
    paddingHorizontal: space.s2,
    minHeight: 56,
  },
  input: { flex: 1, ...type.body, paddingVertical: space.s1 },
  row: { flexDirection: "row", alignItems: "flex-start", gap: space.s2 - 4 },
  status: { flexDirection: "row", alignItems: "center", gap: space.s0 + 2, marginTop: space.s0 },
});
