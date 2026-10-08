/**
 * Sign in (production API). Email + password; the token goes to the Keychain /
 * Keystore through the auth store. New owners create their account on the
 * web (the sign-up asks for company details the phone doesn't need to type).
 */
import { useRef, useState } from "react";
import { KeyboardAvoidingView, Linking, Platform, Pressable, StyleSheet, TextInput, View } from "react-native";
import { useServices } from "../app/servicesContext";
import { SUPPORT_EMAIL, WEB_URL } from "../config";
import { copy } from "../copy";
import { colors, radius, space, type } from "../theme/tokens";
import { Banner, Button, FadeIn, Screen, T } from "../ui/primitives";

export function SignInScreen({ reason }: { reason: "none" | "expired" | "signedOut" }) {
  const { auth } = useServices();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [shown, setShown] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [forgot, setForgot] = useState(false);
  const passwordRef = useRef<TextInput>(null);

  const submit = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    const outcome = await auth.signIn(email, password);
    // On success the auth gate replaces this screen; nothing else to do here.
    if (!outcome.ok) {
      setBusy(false);
      setError(outcome.message);
      if (outcome.kind === "credentials") passwordRef.current?.focus();
    }
  };

  return (
    <KeyboardAvoidingView style={{ flex: 1 }} behavior={Platform.OS === "ios" ? "padding" : undefined}>
      <Screen>
        <View style={styles.head}>
          <T variant="display" style={{ marginTop: space.s4 }}>
            {copy.auth.title}
          </T>
          <T variant="small">{copy.auth.lead}</T>
        </View>

        {reason === "expired" && !error ? <Banner text={copy.auth.expired} /> : null}
        {error ? (
          <FadeIn key={error}>
            <Banner text={error} tone="attention" />
          </FadeIn>
        ) : null}

        <View style={styles.field}>
          <T variant="bodyStrong">{copy.auth.email}</T>
          <TextInput
            value={email}
            onChangeText={setEmail}
            style={styles.input}
            accessibilityLabel={copy.auth.email}
            autoCapitalize="none"
            autoCorrect={false}
            autoComplete="email"
            textContentType="username"
            keyboardType="email-address"
            inputMode="email"
            returnKeyType="next"
            onSubmitEditing={() => passwordRef.current?.focus()}
            editable={!busy}
          />
        </View>

        <View style={styles.field}>
          <T variant="bodyStrong">{copy.auth.password}</T>
          <View style={styles.passwordRow}>
            <TextInput
              ref={passwordRef}
              value={password}
              onChangeText={setPassword}
              style={[styles.input, { flex: 1, paddingRight: 72 }]}
              accessibilityLabel={copy.auth.password}
              secureTextEntry={!shown}
              autoCapitalize="none"
              autoCorrect={false}
              autoComplete="current-password"
              textContentType="password"
              returnKeyType="go"
              onSubmitEditing={() => void submit()}
              editable={!busy}
            />
            <Pressable
              accessibilityRole="button"
              accessibilityLabel={shown ? `${copy.auth.hide} password` : `${copy.auth.show} password`}
              onPress={() => setShown((v) => !v)}
              style={styles.toggle}
              hitSlop={8}
            >
              <T variant="meta">{shown ? copy.auth.hide : copy.auth.show}</T>
            </Pressable>
          </View>
        </View>

        <View style={{ marginTop: space.s1 }}>
          <Button label={copy.auth.submit} busy={busy} onPress={() => void submit()} />
        </View>

        <View style={styles.aside}>
          <Pressable accessibilityRole="button" accessibilityState={{ expanded: forgot }} onPress={() => setForgot((v) => !v)} hitSlop={6}>
            <T variant="small">{copy.auth.forgot}</T>
          </Pressable>
          {forgot ? (
            <FadeIn>
              <T variant="small" style={{ color: colors.text }}>
                {copy.auth.forgotBody(SUPPORT_EMAIL)}
              </T>
            </FadeIn>
          ) : null}
          {WEB_URL ? (
            <Pressable
              accessibilityRole="link"
              accessibilityHint={copy.auth.createAccountHint}
              onPress={() => void Linking.openURL(`${WEB_URL}/signup`)}
              hitSlop={6}
              style={styles.link}
            >
              <T variant="small">{copy.auth.newHere} </T>
              <T variant="bodyStrong" style={styles.underline}>
                {copy.auth.createAccount}
              </T>
            </Pressable>
          ) : null}
        </View>
      </Screen>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  head: { gap: space.s1, marginBottom: space.s3 },
  field: { gap: space.s1, marginBottom: space.s2 },
  input: {
    ...type.body,
    minHeight: 50,
    borderRadius: radius.control,
    borderWidth: 1,
    borderColor: colors.line2,
    backgroundColor: colors.surface,
    paddingHorizontal: space.s2 - 2,
    paddingVertical: space.s1 + 2,
  },
  passwordRow: { flexDirection: "row", alignItems: "center" },
  toggle: { position: "absolute", right: space.s1, paddingHorizontal: space.s1, paddingVertical: space.s1 },
  aside: {
    gap: space.s2,
    marginTop: space.s4,
    paddingTop: space.s3,
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.line2,
  },
  link: { flexDirection: "row", flexWrap: "wrap", alignItems: "baseline" },
  underline: { textDecorationLine: "underline", fontSize: 15 },
});
