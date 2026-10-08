/**
 * Needs You cards (§34-41). Decisions, not forms.
 *
 * Choice: one tap answers; the "remember" box teaches the rule (§5 "I will
 * remember this."). Approval (changed bank details, §26): check by phone, tick,
 * confirm identity again (§52), then send. Approvals are never remembered.
 */
import { useEffect, useRef, useState } from "react";
import { StyleSheet, View } from "react-native";
import type { DecisionOption, NeedsYouApprovalItem, NeedsYouChoiceItem, NeedsYouItem } from "../api/types";
import { useServices } from "../app/servicesContext";
import { copy } from "../copy";
import { formatDay } from "../lib/dates";
import { formatMoney } from "../lib/money";
import { approvalAction, choiceDoneMessage, rememberFlag, rememberLabel } from "../models/needs";
import { colors, space, toneColors } from "../theme/tokens";
import { Icon } from "../ui/Icon";
import { Banner, Bullets, Button, Card, Checkbox, Disclosure, Dot, FadeIn, T } from "../ui/primitives";

/** How long "Done." stays before the card leaves. */
const HOLD_MS = 1600;

type Phase = { kind: "open" } | { kind: "sending"; optionId: string } | { kind: "done"; message: string };

function useResolution(onResolved: () => void) {
  const [phase, setPhase] = useState<Phase>({ kind: "open" });
  const [failed, setFailed] = useState(false);
  // A ref, so a parent re-render (new callback) does not restart the timer.
  const resolvedRef = useRef(onResolved);
  resolvedRef.current = onResolved;
  useEffect(() => {
    if (phase.kind !== "done") return;
    const t = setTimeout(() => resolvedRef.current(), HOLD_MS);
    return () => clearTimeout(t);
  }, [phase]);
  return { phase, setPhase, failed, setFailed };
}

export function DecisionCard({ item, onResolved }: { item: NeedsYouItem; onResolved: () => void }) {
  return item.kind === "choice" ? (
    <ChoiceCard item={item} onResolved={onResolved} />
  ) : (
    <ApprovalCard item={item} onResolved={onResolved} />
  );
}

function Header({ item, detail }: { item: NeedsYouItem; detail?: string | undefined }) {
  const tone = toneColors(item.tone);
  const amount = item.amount ? formatMoney(item.amount, item.currency) : "";
  const meta = [formatDay(item.date), detail].filter(Boolean).join(" · ");
  return (
    <View style={{ gap: space.s0 }}>
      {item.eyebrow ? (
        <View style={styles.eyebrow}>
          <Dot tone={item.tone} size={7} />
          <T variant="meta" style={{ color: item.tone === "neutral" ? colors.text2 : tone.fg }}>
            {item.eyebrow}
          </T>
        </View>
      ) : null}
      <View style={styles.titleRow}>
        <T variant="title" style={{ flex: 1 }}>
          {item.merchant}
        </T>
        {amount ? <T variant="title">{amount}</T> : null}
      </View>
      {meta ? <T variant="meta">{meta}</T> : null}
    </View>
  );
}

function Done({ message }: { message: string }) {
  return (
    <FadeIn>
      <View style={styles.done} accessibilityLiveRegion="polite">
        <Icon name="check" size={20} color={colors.good} strokeWidth={2.2} />
        <T variant="bodyStrong" style={{ flex: 1 }}>
          {message}
        </T>
      </View>
    </FadeIn>
  );
}

function ChoiceCard({ item, onResolved }: { item: NeedsYouChoiceItem; onResolved: () => void }) {
  const { api } = useServices();
  const { phase, setPhase, failed, setFailed } = useResolution(onResolved);
  const [remember, setRemember] = useState(item.remember?.defaultChecked ?? false);
  const [expanded, setExpanded] = useState<string | null>(null);

  const answer = async (optionId: string) => {
    if (phase.kind !== "open") return;
    setFailed(false);
    setPhase({ kind: "sending", optionId });
    const keep = rememberFlag(item, remember);
    const res = await api.answer(item.id, optionId, keep);
    if (res.ok) setPhase({ kind: "done", message: choiceDoneMessage(item, keep) });
    else {
      setFailed(true);
      setPhase({ kind: "open" });
    }
  };

  const tap = (option: DecisionOption) => {
    if (option.choices?.length) setExpanded((cur) => (cur === option.id ? null : option.id));
    else void answer(option.id);
  };

  if (phase.kind === "done") {
    return (
      <Card>
        <Done message={phase.message} />
      </Card>
    );
  }
  const busyId = phase.kind === "sending" ? phase.optionId : null;
  return (
    <Card style={{ gap: space.s2 }}>
      <Header item={item} detail={item.paidWith ? `paid with ${item.paidWith}` : undefined} />
      <T variant="body">{item.question}</T>
      <View style={{ gap: space.s1 }}>
        {item.options.map((option) => (
          <View key={option.id} style={{ gap: space.s1 }}>
            <Button
              kind="secondary"
              label={option.label}
              busy={busyId === option.id}
              disabled={busyId !== null}
              onPress={() => tap(option)}
            />
            {option.choices && expanded === option.id ? (
              <FadeIn>
                <View style={styles.subChoices}>
                  {option.choices.map((c) => (
                    <View key={c.id} style={{ flex: 1 }}>
                      <Button
                        kind="primary"
                        label={c.label}
                        busy={busyId === c.id}
                        disabled={busyId !== null}
                        onPress={() => void answer(c.id)}
                      />
                    </View>
                  ))}
                </View>
              </FadeIn>
            ) : null}
          </View>
        ))}
      </View>
      {item.remember ? <Checkbox checked={remember} onChange={setRemember} label={rememberLabel(item.remember)} /> : null}
      {failed ? <Banner text={copy.couldNotSend} tone="attention" /> : null}
      {item.why.length ? (
        <Disclosure summary={copy.needs.why}>
          <Bullets items={item.why} />
        </Disclosure>
      ) : null}
    </Card>
  );
}

function ApprovalCard({ item, onResolved }: { item: NeedsYouApprovalItem; onResolved: () => void }) {
  const { api, lock } = useServices();
  const { phase, setPhase, failed, setFailed } = useResolution(onResolved);
  const [verifying, setVerifying] = useState(false);
  const [phoneConfirmed, setPhoneConfirmed] = useState(false);

  const send = async (choice: "keep" | "release") => {
    const action = approvalAction(item, choice, phoneConfirmed);
    if (!action || phase.kind !== "open") return;
    setFailed(false);
    if (action.kind === "release") {
      // Hard approval: confirm it is the owner, right now (§25, §52).
      const ok = await lock.confirm(copy.needs.confirmIdentity);
      if (!ok) return;
    }
    setPhase({ kind: "sending", optionId: action.optionId });
    // Never remembered: changed bank details are always a human decision (§26).
    const res = await api.answer(item.id, action.optionId, rememberFlag(item, false));
    if (res.ok) setPhase({ kind: "done", message: action.message });
    else {
      setFailed(true);
      setPhase({ kind: "open" });
    }
  };

  if (phase.kind === "done") {
    return (
      <Card>
        <Done message={phase.message} />
      </Card>
    );
  }
  const busy = phase.kind === "sending" ? phase.optionId : null;
  return (
    <Card style={{ gap: space.s2 }}>
      <Header item={item} />
      <T variant="heading">{item.title}</T>
      <T variant="body">{item.body}</T>
      {item.facts.length ? (
        <View style={styles.facts}>
          {item.facts.map((f) => (
            <View key={f.label} style={styles.fact}>
              <T variant="meta">{f.label}</T>
              <T variant="bodyStrong" style={f.tone === "risk" ? { color: colors.risk } : undefined}>
                {f.value}
              </T>
            </View>
          ))}
        </View>
      ) : null}

      {verifying ? (
        <FadeIn>
          <View style={{ gap: space.s1 + 4 }}>
            <View style={styles.instruction}>
              <Icon name="phone" size={20} color={colors.text} />
              <T variant="body" style={{ flex: 1 }}>
                {item.verification.instruction}
              </T>
            </View>
            <Checkbox checked={phoneConfirmed} onChange={setPhoneConfirmed} label={item.verification.checkboxLabel} />
            <Button
              label={item.verification.confirmLabel}
              disabled={!phoneConfirmed || busy !== null}
              busy={busy === item.verification.confirmOptionId}
              onPress={() => void send("release")}
            />
            <Button kind="secondary" label={item.keepBlocked.label} disabled={busy !== null} onPress={() => void send("keep")} />
          </View>
        </FadeIn>
      ) : (
        <View style={{ gap: space.s1 }}>
          <Button label={item.verification.optionLabel} onPress={() => setVerifying(true)} />
          <Button
            kind="secondary"
            label={item.keepBlocked.label}
            busy={busy === item.keepBlocked.optionId}
            disabled={busy !== null}
            onPress={() => void send("keep")}
          />
        </View>
      )}
      {failed ? <Banner text={copy.couldNotSend} tone="attention" /> : null}
      {item.why.length ? (
        <Disclosure summary={copy.needs.why}>
          <Bullets items={item.why} />
        </Disclosure>
      ) : null}
    </Card>
  );
}

const styles = StyleSheet.create({
  eyebrow: { flexDirection: "row", alignItems: "center", gap: space.s0 + 2 },
  titleRow: { flexDirection: "row", alignItems: "baseline", gap: space.s1 },
  subChoices: { flexDirection: "row", gap: space.s1, paddingLeft: space.s2 },
  done: { flexDirection: "row", alignItems: "center", gap: space.s1 + 2, paddingVertical: space.s1 },
  facts: { backgroundColor: colors.surfaceMuted, borderRadius: 10, padding: space.s2 - 4, gap: space.s1 },
  fact: { gap: 2 },
  instruction: { flexDirection: "row", gap: space.s1 + 2, alignItems: "flex-start" },
});
