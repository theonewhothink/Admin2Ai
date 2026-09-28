/** expo-local-authentication behind the Authenticator port (§52). */
import * as LocalAuthentication from "expo-local-authentication";
import { mapAuthError, type Authenticator, type SecurityLevel } from "./lock";

export const deviceAuthenticator: Authenticator = {
  async securityLevel(): Promise<SecurityLevel> {
    const level = await LocalAuthentication.getEnrolledLevelAsync();
    if (level === LocalAuthentication.SecurityLevel.NONE) return "none";
    if (level === LocalAuthentication.SecurityLevel.SECRET) return "passcode";
    return "biometric";
  },
  async authenticate(prompt: string) {
    const result = await LocalAuthentication.authenticateAsync({
      promptMessage: prompt,
      cancelLabel: "Cancel",
      // Fall back to the phone passcode when Face ID / fingerprint fails.
      disableDeviceFallback: false,
    });
    return result.success ? { ok: true as const } : { ok: false as const, reason: mapAuthError(result.error) };
  },
};
