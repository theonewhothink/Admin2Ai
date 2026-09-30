/**
 * Scan (§11). One action. The native scanner finds the edges, crops,
 * straightens and takes as many pages as needed. If every page looks fine it is
 * sent at once; otherwise the owner is asked, in one sentence, whether to retake.
 * Nothing is typed: no category, no amount, no supplier.
 */
import * as Crypto from "expo-crypto";
import { useState } from "react";
import { Alert, StyleSheet, View } from "react-native";
import { useQueueSummary } from "../app/hooks";
import { useServices } from "../app/servicesContext";
import { copy } from "../copy";
import { localDayKey } from "../lib/dates";
import { discardTemporary, readFileBytes } from "../offline/expo/files";
import { expoNetwork } from "../offline/expo/network";
import type { QueueSummary } from "../offline/pipeline";
import { devicePageAnalyzer, deviceQrDetector, nativeDocumentScanner } from "../scan/expo";
import { inspectPages, needsReview, replacePage, reviewNotes, saveMessage, savePages, type ReviewedPage } from "../scan/session";
import { colors, space } from "../theme/tokens";
import { Icon } from "../ui/Icon";
import { Banner, Button, Card, FadeIn, Screen, T } from "../ui/primitives";

type Step =
  | { kind: "idle"; message?: string }
  | { kind: "working" }
  | { kind: "review"; pages: ReviewedPage[] };

export function ScanScreen() {
  const { offline, api } = useServices();
  const summary = useQueueSummary();
  const [step, setStep] = useState<Step>({ kind: "idle" });

  const save = async (pages: ReviewedPage[]) => {
    setStep({ kind: "working" });
    const result = await savePages(pages, {
      sink: offline.pipeline,
      readFile: readFileBytes,
      discard: discardTemporary,
      newCaptureId: () => Crypto.randomUUID(),
      now: () => new Date(),
    });
    // Demo mode has no server: never claim the scan is on its way.
    const online = !api.isDemo && (await expoNetwork.isOnline().catch(() => false));
    setStep({ kind: "idle", message: saveMessage(result, online) });
    void offline.runner.kick();
  };

  /** Never leave the screen stuck on "working": any unexpected error becomes one calm sentence. */
  const guarded = (task: () => Promise<void>) => async () => {
    try {
      await task();
    } catch {
      setStep({ kind: "idle", message: copy.scan.failed });
    }
  };

  const scan = guarded(async () => {
    setStep({ kind: "working" });
    const outcome = await nativeDocumentScanner.scan();
    if (outcome.status === "unavailable") return setStep({ kind: "idle", message: copy.scan.unavailable });
    if (outcome.status === "cancelled") return setStep({ kind: "idle" });
    const pages = await inspectPages(outcome.pages, devicePageAnalyzer, deviceQrDetector);
    if (needsReview(pages)) setStep({ kind: "review", pages });
    else await save(pages);
  });

  const retake = (pages: ReviewedPage[], index: number) =>
    guarded(async () => {
      setStep({ kind: "working" });
      const outcome = await nativeDocumentScanner.scan({ maxPages: 1 });
      if (outcome.status !== "captured") return setStep({ kind: "review", pages });
      const [fresh] = await inspectPages(outcome.pages.slice(0, 1), devicePageAnalyzer, deviceQrDetector);
      if (!fresh) return setStep({ kind: "review", pages });
      const next = replacePage(pages, index, fresh, discardTemporary);
      if (needsReview(next)) setStep({ kind: "review", pages: next });
      else await save(next);
    })();

  /** The owner decided not to send: remove the plaintext pages. */
  const cancel = (pages: ReviewedPage[]) => {
    for (const page of pages) discardTemporary(page.uri);
    setStep({ kind: "idle" });
  };

  return (
    <Screen title={copy.scan.title}>
      {step.kind === "review" ? (
        <Review
          pages={step.pages}
          onRetake={(i) => void retake(step.pages, i)}
          onUse={() => void guarded(() => save(step.pages))()}
          onCancel={() => cancel(step.pages)}
        />
      ) : (
        <Card style={styles.hero}>
          <View style={styles.heroIcon}>
            <Icon name="scan" size={34} color={colors.text} />
          </View>
          <T variant="body" style={{ textAlign: "center" }}>
            {copy.scan.hint}
          </T>
          <T variant="small" style={{ textAlign: "center" }}>
            {copy.scan.noTyping}
          </T>
          <View style={{ alignSelf: "stretch" }}>
            <Button label={copy.scan.action} busy={step.kind === "working"} onPress={() => void scan()} />
          </View>
        </Card>
      )}
      {step.kind === "idle" && step.message ? (
        <FadeIn key={step.message}>
          <View style={styles.message} accessibilityLiveRegion="polite">
            <T variant="bodyStrong">{step.message}</T>
          </View>
        </FadeIn>
      ) : null}
      {summary ? <QueueStatus summary={summary} /> : null}
    </Screen>
  );
}

function Review({
  pages,
  onRetake,
  onUse,
  onCancel,
}: {
  pages: ReviewedPage[];
  onRetake: (index: number) => void;
  onUse: () => void;
  onCancel: () => void;
}) {
  const notes = reviewNotes(pages);
  const retakePages = [...new Set(notes.map((n) => n.page))];
  return (
    <FadeIn>
      <Card style={{ gap: space.s2 }}>
        {notes.map((n) => (
          <T key={`${n.page}-${n.text}`} variant="body">
            {n.text}
          </T>
        ))}
        <View style={{ gap: space.s1 }}>
          {retakePages.map((page) => (
            <Button key={page} kind="secondary" label={copy.scan.retake(page)} onPress={() => onRetake(page - 1)} />
          ))}
          <Button label={copy.scan.useAnyway} onPress={onUse} />
          <Button kind="quiet" label={copy.scan.cancel} onPress={onCancel} />
        </View>
      </Card>
    </FadeIn>
  );
}

function QueueStatus({ summary }: { summary: QueueSummary }) {
  const { offline } = useServices();
  const today = localDayKey(new Date());
  const sentToday = summary.sent.filter((s) => localDayKey(new Date(s.verifiedAt)) === today).length;
  const lines = [summary.waiting > 0 ? copy.scan.waiting(summary.waiting) : null, sentToday > 0 ? copy.scan.sentToday(sentToday) : null].filter(
    (l): l is string => l !== null,
  );

  const remove = (id: string) =>
    Alert.alert(copy.held.remove, copy.held.removeConfirm, [
      { text: copy.held.cancel, style: "cancel" },
      { text: copy.held.remove, style: "destructive", onPress: () => void offline.pipeline.discardHeld(id) },
    ]);

  return (
    <View style={{ marginTop: space.s3, gap: space.s1 }}>
      {lines.map((l) => (
        <T key={l} variant="small">
          {l}
        </T>
      ))}
      {summary.held.length > 0 ? (
        <View style={{ marginTop: space.s1, gap: space.s1 }}>
          <Banner text={copy.held.summary(summary.held.length)} tone="attention" />
          {summary.held.map((h) => (
            <Card key={h.id} style={{ gap: space.s1 }}>
              <T variant="bodyStrong" numberOfLines={1}>
                {h.fileName}
              </T>
              <T variant="small">{copy.held.reason(h.reason)}</T>
              <View style={styles.heldActions}>
                <View style={{ flex: 1 }}>
                  <Button
                    kind="secondary"
                    label={copy.held.retry}
                    onPress={() => {
                      void offline.pipeline.retryHeld(h.id).then(() => offline.runner.kick());
                    }}
                  />
                </View>
                <View style={{ flex: 1 }}>
                  <Button kind="quiet" label={copy.held.remove} onPress={() => remove(h.id)} />
                </View>
              </View>
            </Card>
          ))}
        </View>
      ) : null}
    </View>
  );
}

const styles = StyleSheet.create({
  hero: { alignItems: "center", gap: space.s2, paddingVertical: space.s4 },
  heroIcon: {
    width: 72,
    height: 72,
    borderRadius: 36,
    backgroundColor: colors.surfaceMuted,
    alignItems: "center",
    justifyContent: "center",
  },
  message: { marginTop: space.s2 },
  heldActions: { flexDirection: "row", gap: space.s1 },
});
