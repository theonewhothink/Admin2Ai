/**
 * AES-256-GCM and sha256 on the device via expo-crypto (native CryptoKit on iOS,
 * javax.crypto on Android). The key lives in expo-secure-store (Keychain /
 * Android Keystore) and never touches disk in the clear (§43, §52).
 */
import * as Crypto from "expo-crypto";
import * as SecureStore from "expo-secure-store";
import { fromBase64, ownedBytes, toBase64, toHex } from "../../lib/bytes";
import { frameSealed, GCM_NONCE_BYTES, GCM_TAG_BYTES, unframeSealed } from "../envelope";
import type { Cipher, Hasher } from "../types";

const KEY_NAME = "backoffice.evidence-key.v1";
const KEY_BYTES = 32;

/**
 * Readable after the first unlock so background uploads work while the phone is
 * locked; never synced to other devices or backups.
 */
const KEY_OPTIONS: SecureStore.SecureStoreOptions = {
  keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
};

let keyPromise: Promise<Uint8Array> | null = null;

/** Load the evidence key, creating it on first use. Concurrent callers share one promise. */
export function loadOrCreateEvidenceKey(): Promise<Uint8Array> {
  if (!keyPromise) {
    keyPromise = (async () => {
      const stored = await SecureStore.getItemAsync(KEY_NAME, KEY_OPTIONS);
      if (stored) {
        const bytes = fromBase64(stored);
        // Refuse to silently replace a damaged key: that would make queued evidence unreadable.
        if (bytes.length !== KEY_BYTES) throw new Error("stored evidence key has the wrong length");
        return bytes;
      }
      const fresh = Crypto.getRandomBytes(KEY_BYTES);
      await SecureStore.setItemAsync(KEY_NAME, toBase64(fresh), KEY_OPTIONS);
      return fresh;
    })();
    keyPromise.catch(() => {
      keyPromise = null; // Allow a retry, e.g. if the keychain was briefly unavailable.
    });
  }
  return keyPromise;
}

export class ExpoAesGcmCipher implements Cipher {
  private key: Promise<Crypto.AESEncryptionKey> | null = null;

  constructor(private readonly loadKey: () => Promise<Uint8Array>) {}

  async seal(plaintext: Uint8Array, associatedData: Uint8Array): Promise<Uint8Array> {
    const sealed = await Crypto.aesEncryptAsync(plaintext, await this.getKey(), {
      nonce: { length: GCM_NONCE_BYTES },
      tagLength: GCM_TAG_BYTES,
      additionalData: associatedData,
    });
    const nonce = await sealed.iv("bytes");
    const ciphertextWithTag = await sealed.ciphertext({ includeTag: true, encoding: "bytes" });
    return frameSealed(nonce, ciphertextWithTag);
  }

  async open(sealed: Uint8Array, associatedData: Uint8Array): Promise<Uint8Array> {
    const { nonce, ciphertextWithTag } = unframeSealed(sealed);
    const data = Crypto.AESSealedData.fromParts(nonce, ciphertextWithTag, GCM_TAG_BYTES);
    return Crypto.aesDecryptAsync(data, await this.getKey(), { additionalData: associatedData, output: "bytes" });
  }

  private getKey(): Promise<Crypto.AESEncryptionKey> {
    if (!this.key) {
      this.key = this.loadKey().then((bytes) => Crypto.AESEncryptionKey.import(bytes));
      this.key.catch(() => {
        this.key = null;
      });
    }
    return this.key;
  }
}

export const expoHasher: Hasher = {
  async sha256Hex(bytes: Uint8Array): Promise<string> {
    const digest = await Crypto.digest(Crypto.CryptoDigestAlgorithm.SHA256, ownedBytes(bytes));
    return toHex(new Uint8Array(digest));
  },
};
