/**
 * Ask (§34-41): "Ask your business anything". Answers point to evidence; when
 * an answer has none, the screen says so (AI memory is never financial evidence).
 */
import { useLocalSearchParams } from "expo-router";
import { useState } from "react";
import { KeyboardAvoidingView, Platform, Pressable, StyleSheet, TextInput, View } from "react-native";
import type { AskAnswer } from "../api/types";
import { useServices } from "../app/servicesContext";
import { copy } from "../copy";
import { colors, radius, space, type } from "../theme/tokens";
import { Icon } from "../ui/Icon";
import { Card, Chip, FadeIn, Screen, T } from "../ui/primitives";

type State = { kind: "idle" } | { kind: "asking" } | { kind: "answer"; question: string; answer: AskAnswer } | { kind: "failed" };

export function AskScreen() {
  const { api } = useServices();
  const { q } = useLocalSearchParams<{ q?: string }>();
  const [question, setQuestion] = useState(typeof q === "string" ? q : "");
  const [state, setState] = useState<State>({ kind: "idle" });
  const [given, setGiven] = useState(q);
  if (q !== given) {
    // Opened with a question ready (Sources' "Ask about it"): it is put in the box, sent by the owner.
    setGiven(q);
    if (typeof q === "string" && q.trim()) setQuestion(q);
  }

  const ask = async (q: string) => {
    const text = q.trim();
    if (!text || state.kind === "asking") return;
    setQuestion(text);
    setState({ kind: "asking" });
    const res = await api.ask(text);
    setState(res.ok ? { kind: "answer", question: text, answer: res.answer } : { kind: "failed" });
  };

  const canSend = question.trim().length > 0 && state.kind !== "asking";
  return (
    <KeyboardAvoidingView style={{ flex: 1, backgroundColor: colors.bg }} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <Screen title={copy.ask.title}>
        <View style={styles.box}>
          <TextInput
            value={question}
            onChangeText={setQuestion}
            placeholder={copy.ask.placeholder}
            placeholderTextColor={colors.text3}
            style={styles.input}
            returnKeyType="send"
            onSubmitEditing={() => void ask(question)}
            accessibilityLabel={copy.ask.placeholder}
            maxLength={500}
          />
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={copy.ask.send}
            disabled={!canSend}
            onPress={() => void ask(question)}
            style={[styles.send, !canSend && { opacity: 0.35 }]}
          >
            <Icon name="arrowUp" size={20} color={colors.onInk} strokeWidth={2} />
          </Pressable>
        </View>

        {state.kind === "idle" ? (
          <View style={styles.examples}>
            {copy.ask.examples.map((e) => (
              <Chip key={e} label={e} onPress={() => void ask(e)} />
            ))}
          </View>
        ) : null}
        {state.kind === "asking" ? (
          <T variant="small" style={{ marginTop: space.s2 }}>
            {copy.ask.thinking}
          </T>
        ) : null}
        {state.kind === "failed" ? (
          <T variant="small" style={{ marginTop: space.s2 }}>
            {copy.ask.offline}
          </T>
        ) : null}
        {state.kind === "answer" ? (
          <FadeIn key={state.question}>
            <Card style={{ marginTop: space.s2, gap: space.s2 }}>
              <T variant="body">{state.answer.answer}</T>
              {state.answer.evidence.length ? (
                <View style={styles.examples}>
                  {state.answer.evidence.map((e) => (
                    <View key={e.id} style={styles.evidence}>
                      <Icon name="document" size={14} color={colors.text2} />
                      <T variant="meta" style={{ color: colors.text }}>
                        {e.label}
                      </T>
                    </View>
                  ))}
                </View>
              ) : (
                <T variant="meta">{copy.ask.noEvidence}</T>
              )}
            </Card>
          </FadeIn>
        ) : null}
      </Screen>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  box: {
    flexDirection: "row",
    alignItems: "center",
    backgroundColor: colors.surface,
    borderRadius: radius.card,
    borderWidth: 1,
    borderColor: colors.line2,
    paddingLeft: space.s2,
    paddingRight: space.s1,
    minHeight: 56,
  },
  input: { flex: 1, ...type.body, paddingVertical: space.s1 },
  send: {
    width: 40,
    height: 40,
    borderRadius: 20,
    backgroundColor: colors.ink,
    alignItems: "center",
    justifyContent: "center",
  },
  examples: { flexDirection: "row", flexWrap: "wrap", gap: space.s1, marginTop: space.s2 },
  evidence: {
    flexDirection: "row",
    alignItems: "center",
    gap: space.s0 + 2,
    backgroundColor: colors.surfaceMuted,
    borderRadius: radius.pill,
    paddingHorizontal: space.s1 + 2,
    paddingVertical: space.s0 + 1,
  },
});
