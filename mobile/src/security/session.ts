/**
 * The signed-in owner's API token, kept in the Keychain / Keystore (§52 secrets).
 * Sign-in itself is not part of this phase; until a token is stored the API
 * client sends no Authorization header.
 */
import * as SecureStore from "expo-secure-store";

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
