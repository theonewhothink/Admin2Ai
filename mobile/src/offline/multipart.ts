/**
 * multipart/form-data encoder (RFC 7578) producing raw bytes.
 *
 * React Native's FormData cannot carry in-memory bytes, and the evidence is
 * decrypted in memory just before upload (§43), so the body is built here and
 * sent as a Uint8Array.
 */
import { concatBytes, utf8Encode } from "../lib/bytes";

export interface MultipartField {
  name: string;
  value: string;
}

export interface MultipartFile {
  name: string;
  fileName: string;
  contentType: string;
  bytes: Uint8Array;
}

const CRLF = "\r\n";
const BOUNDARY_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";

/** Escape a header parameter the way browsers do (HTML spec form encoding). */
export function escapeParam(value: string): string {
  return value.replace(/\r/g, "%0D").replace(/\n/g, "%0A").replace(/"/g, "%22");
}

/** Keep content types to a safe token set; fall back to octet-stream. */
export function safeContentType(value: string): string {
  const v = value.trim().toLowerCase();
  return /^[a-z0-9!#$&^_.+-]+\/[a-z0-9!#$&^_.+-]+(\s*;\s*[a-z0-9_-]+=[a-z0-9_.-]+)*$/.test(v)
    ? v
    : "application/octet-stream";
}

export function makeBoundary(random: () => number): string {
  let s = "----BackOfficeBoundary";
  for (let i = 0; i < 24; i++) s += BOUNDARY_CHARS[Math.floor(random() * BOUNDARY_CHARS.length)] ?? "0";
  return s;
}

function contains(haystack: Uint8Array, needle: Uint8Array): boolean {
  outer: for (let i = 0; i + needle.length <= haystack.length; i++) {
    for (let j = 0; j < needle.length; j++) {
      if (haystack[i + j] !== needle[j]) continue outer;
    }
    return true;
  }
  return false;
}

/** True when the boundary delimiter does not occur in any part, so it can be used safely. */
export function boundaryIsSafe(boundary: string, fields: readonly MultipartField[], file: MultipartFile): boolean {
  const delimiter = utf8Encode(`--${boundary}`);
  if (contains(file.bytes, delimiter)) return false;
  return fields.every((f) => !f.value.includes(`--${boundary}`));
}

export function encodeMultipart(boundary: string, fields: readonly MultipartField[], file: MultipartFile): Uint8Array {
  const parts: Uint8Array[] = [];
  for (const field of fields) {
    parts.push(
      utf8Encode(
        `--${boundary}${CRLF}` +
          `Content-Disposition: form-data; name="${escapeParam(field.name)}"${CRLF}${CRLF}` +
          `${field.value}${CRLF}`,
      ),
    );
  }
  parts.push(
    utf8Encode(
      `--${boundary}${CRLF}` +
        `Content-Disposition: form-data; name="${escapeParam(file.name)}"; filename="${escapeParam(file.fileName)}"${CRLF}` +
        `Content-Type: ${safeContentType(file.contentType)}${CRLF}${CRLF}`,
    ),
    file.bytes,
    utf8Encode(`${CRLF}--${boundary}--${CRLF}`),
  );
  return concatBytes(parts);
}

/** Pick a random boundary that does not collide with the content (collisions are astronomically rare). */
export function encodeMultipartSafely(
  fields: readonly MultipartField[],
  file: MultipartFile,
  random: () => number,
): { boundary: string; body: Uint8Array } {
  for (let attempt = 0; attempt < 8; attempt++) {
    const boundary = makeBoundary(random);
    if (boundaryIsSafe(boundary, fields, file)) {
      return { boundary, body: encodeMultipart(boundary, fields, file) };
    }
  }
  throw new Error("could not find a safe multipart boundary");
}
