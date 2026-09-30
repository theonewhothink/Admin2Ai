/**
 * Binary layouts for data at rest (§43, §52 encryption at rest).
 *
 * Evidence envelope (plaintext, before sealing):
 *   "BOE1" | uint32 metaLength | meta JSON (UTF-8) | original bytes
 * Keeping the metadata inside the sealed blob means a blob can be re-registered
 * after a crash even if the queue journal was never written. Evidence is never lost.
 *
 * Sealed frame (what is written to disk):
 *   0x01 (version) | 12-byte nonce | ciphertext | 16-byte GCM tag
 */
import { concatBytes, readUint32, utf8Decode, utf8Encode, writeUint32 } from "../lib/bytes";
import type { CaptureMeta, CaptureSource, CaptureFormat } from "./types";

const MAGIC = utf8Encode("BOE1");

export const SEALED_VERSION = 1;
export const GCM_NONCE_BYTES = 12;
export const GCM_TAG_BYTES = 16;

export function encodeEnvelope(meta: CaptureMeta, bytes: Uint8Array): Uint8Array {
  const metaBytes = utf8Encode(JSON.stringify(meta));
  return concatBytes([MAGIC, writeUint32(metaBytes.length), metaBytes, bytes]);
}

export function decodeEnvelope(envelope: Uint8Array): { meta: CaptureMeta; bytes: Uint8Array } {
  if (envelope.length < MAGIC.length + 4) throw new Error("envelope too short");
  for (let i = 0; i < MAGIC.length; i++) {
    if (envelope[i] !== MAGIC[i]) throw new Error("envelope magic mismatch");
  }
  const metaLength = readUint32(envelope, MAGIC.length);
  const metaStart = MAGIC.length + 4;
  const metaEnd = metaStart + metaLength;
  if (metaEnd > envelope.length) throw new Error("envelope meta out of bounds");
  const meta = parseCaptureMeta(JSON.parse(utf8Decode(envelope.subarray(metaStart, metaEnd))));
  if (!meta) throw new Error("envelope meta invalid");
  return { meta, bytes: envelope.slice(metaEnd) };
}

const SOURCES: readonly CaptureSource[] = ["mobile_scan", "mobile_share"];
const FORMATS: readonly CaptureFormat[] = ["pdf", "image", "screenshot", "url", "eml", "text", "xml"];

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Validate metadata read back from disk. Returns null when anything required is missing. */
export function parseCaptureMeta(value: unknown): CaptureMeta | null {
  if (!isRecord(value)) return null;
  const { source, format, fileName, mimeType, capturedAt } = value;
  if (!SOURCES.includes(source as CaptureSource) || !FORMATS.includes(format as CaptureFormat)) return null;
  if (typeof fileName !== "string" || typeof mimeType !== "string" || typeof capturedAt !== "string") return null;
  return value as unknown as CaptureMeta;
}

/** Wrap nonce + ciphertext-with-tag into a versioned frame. */
export function frameSealed(nonce: Uint8Array, ciphertextWithTag: Uint8Array): Uint8Array {
  if (nonce.length !== GCM_NONCE_BYTES) throw new Error("nonce must be 12 bytes");
  if (ciphertextWithTag.length < GCM_TAG_BYTES) throw new Error("ciphertext shorter than tag");
  return concatBytes([Uint8Array.of(SEALED_VERSION), nonce, ciphertextWithTag]);
}

/** Split a versioned frame. Throws on unknown versions rather than guessing. */
export function unframeSealed(sealed: Uint8Array): { nonce: Uint8Array; ciphertextWithTag: Uint8Array } {
  if (sealed.length < 1 + GCM_NONCE_BYTES + GCM_TAG_BYTES) throw new Error("sealed data too short");
  if (sealed[0] !== SEALED_VERSION) throw new Error(`unsupported sealed version ${String(sealed[0])}`);
  return {
    nonce: sealed.slice(1, 1 + GCM_NONCE_BYTES),
    ciphertextWithTag: sealed.slice(1 + GCM_NONCE_BYTES),
  };
}
