/**
 * The signed-in owner's API token, kept in the Keychain / Keystore (§52
 * secrets), never in plain files. Written by the auth store (src/auth) after
 * sign-in, deleted on sign-out or when the server answers 401. Also small
 * non-secret flags (notification choices) under the same protection.
 */
import * as SecureStore from "expo-secure-store";
import type { TokenStorage } from "../auth/store";
import type { PushPrefs } from "../notifications/push";

const TOKEN_KEY = "backoffice.session-token.v1";
const OPTIONS: SecureStore.SecureStoreOptions = {
  keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
};

export async function getSessionToken(): Promise<string | null> {
  try {
    return await SecureStore.getItemAsync(TOKEN_KEY, OPTIONS);
  } catch {
    return null;
  }
}

export function setSessionToken(token: string): Promise<void> {
  return SecureStore.setItemAsync(TOKEN_KEY, token, OPTIONS);
}

export function clearSessionToken(): Promise<void> {
  return SecureStore.deleteItemAsync(TOKEN_KEY, OPTIONS);
}

export const secureTokenStorage: TokenStorage = {
  get: getSessionToken,
  set: setSessionToken,
  clear: clearSessionToken,
};

export const securePrefs: PushPrefs = {
  get: (key) => SecureStore.getItemAsync(key, OPTIONS),
  set: (key, value) => SecureStore.setItemAsync(key, value, OPTIONS),
  remove: (key) => SecureStore.deleteItemAsync(key, OPTIONS),
};
