/**
 * Byte helpers with no platform dependencies.
 *
 * Hermes does not guarantee `TextDecoder`, `atob` or `Buffer`, so the offline
 * pipeline (§43) uses these small, tested implementations instead.
 */

/** Encode a string as UTF-8. Lone surrogates become U+FFFD, like TextEncoder. */
export function utf8Encode(text: string): Uint8Array {
  const out: number[] = [];
  for (let i = 0; i < text.length; i++) {
    let code = text.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = i + 1 < text.length ? text.charCodeAt(i + 1) : 0;
      if (next >= 0xdc00 && next <= 0xdfff) {
        code = 0x10000 + ((code - 0xd800) << 10) + (next - 0xdc00);
        i++;
      } else {
        code = 0xfffd;
      }
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      code = 0xfffd;
    }
    if (code < 0x80) {
      out.push(code);
    } else if (code < 0x800) {
      out.push(0xc0 | (code >> 6), 0x80 | (code & 0x3f));
    } else if (code < 0x10000) {
      out.push(0xe0 | (code >> 12), 0x80 | ((code >> 6) & 0x3f), 0x80 | (code & 0x3f));
    } else {
      out.push(
        0xf0 | (code >> 18),
        0x80 | ((code >> 12) & 0x3f),
        0x80 | ((code >> 6) & 0x3f),
        0x80 | (code & 0x3f),
      );
    }
  }
  return Uint8Array.from(out);
}

/** Decode UTF-8. Malformed sequences become U+FFFD instead of throwing. */
export function utf8Decode(bytes: Uint8Array): string {
  let out = "";
  let i = 0;
  while (i < bytes.length) {
    const b0 = bytes[i] as number;
    let code = 0xfffd;
    let size = 1;
    if (b0 < 0x80) {
      code = b0;
    } else if (b0 >= 0xc2 && b0 <= 0xdf && isCont(bytes, i + 1)) {
      code = ((b0 & 0x1f) << 6) | ((bytes[i + 1] as number) & 0x3f);
      size = 2;
    } else if (b0 >= 0xe0 && b0 <= 0xef && isCont(bytes, i + 1) && isCont(bytes, i + 2)) {
      const c = ((b0 & 0x0f) << 12) | (((bytes[i + 1] as number) & 0x3f) << 6) | ((bytes[i + 2] as number) & 0x3f);
      if (c >= 0x800 && (c < 0xd800 || c > 0xdfff)) {
        code = c;
        size = 3;
      }
    } else if (b0 >= 0xf0 && b0 <= 0xf4 && isCont(bytes, i + 1) && isCont(bytes, i + 2) && isCont(bytes, i + 3)) {
      const c =
        ((b0 & 0x07) << 18) |
        (((bytes[i + 1] as number) & 0x3f) << 12) |
        (((bytes[i + 2] as number) & 0x3f) << 6) |
        ((bytes[i + 3] as number) & 0x3f);
      if (c >= 0x10000 && c <= 0x10ffff) {
        code = c;
        size = 4;
      }
    }
    out += String.fromCodePoint(code);
    i += size;
  }
  return out;
}

function isCont(bytes: Uint8Array, index: number): boolean {
  return index < bytes.length && ((bytes[index] as number) & 0xc0) === 0x80;
}

const HEX = "0123456789abcdef";

/** Lower-case hex, the form the backend uses for sha256 (Evidence.hash_bytes). */
export function toHex(bytes: Uint8Array): string {
  let out = "";
  for (const b of bytes) out += HEX[b >> 4]! + HEX[b & 0x0f]!;
  return out;
}

/** Normalise a hex digest for comparison: trimmed and lower-case. Returns null if not hex. */
export function normalizeHexDigest(value: string, byteLength = 32): string | null {
  const v = value.trim().toLowerCase();
  return v.length === byteLength * 2 && /^[0-9a-f]+$/.test(v) ? v : null;
}

const B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

export function toBase64(bytes: Uint8Array): string {
  let out = "";
  let i = 0;
  for (; i + 2 < bytes.length; i += 3) {
    const n = ((bytes[i] as number) << 16) | ((bytes[i + 1] as number) << 8) | (bytes[i + 2] as number);
    out += B64[n >> 18]! + B64[(n >> 12) & 63]! + B64[(n >> 6) & 63]! + B64[n & 63]!;
  }
  const rest = bytes.length - i;
  if (rest === 1) {
    const n = (bytes[i] as number) << 16;
    out += B64[n >> 18]! + B64[(n >> 12) & 63]! + "==";
  } else if (rest === 2) {
    const n = ((bytes[i] as number) << 16) | ((bytes[i + 1] as number) << 8);
    out += B64[n >> 18]! + B64[(n >> 12) & 63]! + B64[(n >> 6) & 63]! + "=";
  }
  return out;
}

/** Strict base64 decode (standard alphabet, padding optional). Throws on invalid input. */
export function fromBase64(text: string): Uint8Array {
  const clean = text.replace(/\s+/g, "").replace(/=+$/, "");
  if (clean.length % 4 === 1 || /[^A-Za-z0-9+/]/.test(clean)) {
    throw new Error("invalid base64");
  }
  const out = new Uint8Array(Math.floor((clean.length * 3) / 4));
  let buffer = 0;
  let bits = 0;
  let o = 0;
  for (const ch of clean) {
    buffer = (buffer << 6) | B64.indexOf(ch);
    bits += 6;
    if (bits >= 8) {
      bits -= 8;
      out[o++] = (buffer >> bits) & 0xff;
    }
  }
  return out;
}

/**
 * View the bytes as `Uint8Array<ArrayBuffer>` (what WebCrypto-style APIs accept).
 * Copies only when the backing store is a SharedArrayBuffer.
 */
export function ownedBytes(bytes: Uint8Array): Uint8Array<ArrayBuffer> {
  return bytes.buffer instanceof ArrayBuffer ? (bytes as Uint8Array<ArrayBuffer>) : new Uint8Array(bytes);
}

export function concatBytes(parts: readonly Uint8Array[]): Uint8Array {
  const total = parts.reduce((n, p) => n + p.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const p of parts) {
    out.set(p, offset);
    offset += p.length;
  }
  return out;
}

export function bytesEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
  return true;
}

/** Big-endian uint32 helpers for small binary headers. */
export function writeUint32(value: number): Uint8Array {
  if (!Number.isInteger(value) || value < 0 || value > 0xffffffff) throw new RangeError("uint32 out of range");
  return Uint8Array.of((value >>> 24) & 0xff, (value >>> 16) & 0xff, (value >>> 8) & 0xff, value & 0xff);
}

export function readUint32(bytes: Uint8Array, offset: number): number {
  if (offset < 0 || offset + 4 > bytes.length) throw new RangeError("uint32 read out of bounds");
  return (
    (((bytes[offset] as number) << 24) >>> 0) +
    ((bytes[offset + 1] as number) << 16) +
    ((bytes[offset + 2] as number) << 8) +
    (bytes[offset + 3] as number)
  );
}
