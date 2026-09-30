/**
 * HTTP transport for POST /api/evidence/upload (§43).
 *
 * Sends sha256 + bytes as multipart/form-data with an Idempotency-Key, then
 * classifies the answer. It never invents a receipt: the pipeline deletes the
 * local copy only when the server's own hash matches.
 */
import { normalizeHexDigest } from "../lib/bytes";
import { authHeaders, isRecord, parseJson, type ApiEndpoint, type HttpSend } from "../api/http";
import { parseRetryAfter } from "./backoff";
import {
  AUTH_STATUSES,
  FILE_FIELD,
  HASH_MISMATCH_ERROR,
  IDEMPOTENCY_HEADER,
  RETRYABLE_STATUSES,
  UPLOAD_PATH,
} from "./contract";
import { encodeMultipartSafely, type MultipartField } from "./multipart";
import type { EvidenceTransport, UploadOutcome, UploadRequest } from "./types";

/** Build the form fields for one upload. Order is stable (tests and golden fixture rely on it). */
export function uploadFields(request: UploadRequest): MultipartField[] {
  const { meta } = request;
  const fields: MultipartField[] = [
    { name: "sha256", value: request.sha256 },
    { name: "source", value: meta.source },
    { name: "format", value: meta.format },
    { name: "captured_at", value: meta.capturedAt },
    { name: "client_item_id", value: request.idempotencyKey },
  ];
  if (meta.captureId) fields.push({ name: "capture_id", value: meta.captureId });
  if (meta.page !== undefined) fields.push({ name: "page", value: String(meta.page) });
  if (meta.pageCount !== undefined) fields.push({ name: "page_count", value: String(meta.pageCount) });
  if (meta.originalUrl) fields.push({ name: "original_url", value: meta.originalUrl });
  if (meta.hints && Object.keys(meta.hints).length > 0) {
    fields.push({ name: "hints", value: JSON.stringify(meta.hints) });
  }
  return fields;
}

export interface ParsedReceipt {
  sha256: string;
  evidenceId?: string;
  duplicate?: boolean;
}

/** `{ "sha256": "<hex>", "evidence_id"?: string, "duplicate"?: boolean }` or null. */
export function parseReceipt(body: unknown): ParsedReceipt | null {
  if (!isRecord(body) || typeof body.sha256 !== "string") return null;
  const sha256 = normalizeHexDigest(body.sha256);
  if (!sha256) return null;
  const receipt: ParsedReceipt = { sha256 };
  if (typeof body.evidence_id === "string" && body.evidence_id) receipt.evidenceId = body.evidence_id;
  if (typeof body.duplicate === "boolean") receipt.duplicate = body.duplicate;
  return receipt;
}

/** Recognise `{ "error": "hash_mismatch" }` or FastAPI's `{ "detail": ... }` wrapping. */
function isHashMismatch(body: unknown): { serverSha256?: string } | null {
  if (!isRecord(body)) return null;
  const candidates: unknown[] = [body, body.detail];
  for (const c of candidates) {
    if (c === HASH_MISMATCH_ERROR) return {};
    if (isRecord(c) && c.error === HASH_MISMATCH_ERROR) {
      const sha = typeof c.sha256 === "string" ? normalizeHexDigest(c.sha256) : null;
      return sha ? { serverSha256: sha } : {};
    }
  }
  return null;
}

/** Map an HTTP status and body to an outcome. Pure; unit-tested. */
export function classifyResponse(status: number, body: unknown, retryAfter: string | null, nowMs: number): UploadOutcome {
  if ((status >= 200 && status < 300) || status === 409) {
    const receipt = parseReceipt(body);
    if (receipt) return { kind: "receipt", ...receipt };
    const mismatch = isHashMismatch(body);
    if (mismatch) return { kind: "mismatch", status, ...mismatch };
    return { kind: "retryable", status };
  }
  const mismatch = isHashMismatch(body);
  if (mismatch) return { kind: "mismatch", status, ...mismatch };
  if (AUTH_STATUSES.includes(status)) return { kind: "auth", status };
  if (RETRYABLE_STATUSES.includes(status) || status >= 500) {
    const retryAfterMs = parseRetryAfter(retryAfter, nowMs);
    return retryAfterMs === undefined ? { kind: "retryable", status } : { kind: "retryable", status, retryAfterMs };
  }
  return { kind: "rejected", status };
}

export interface HttpUploaderOptions {
  endpoint: ApiEndpoint;
  send: HttpSend;
  random: () => number;
  now: () => number;
  /** Uploads can be large; allow longer than ordinary API calls. */
  timeoutMs?: number;
}

export class HttpEvidenceUploader implements EvidenceTransport {
  constructor(private readonly options: HttpUploaderOptions) {}

  async upload(request: UploadRequest): Promise<UploadOutcome> {
    const { endpoint, send, random, now } = this.options;
    // Demo mode has no server, so there is nobody to issue a receipt. Stay queued.
    if (!endpoint.baseUrl) return { kind: "network" };
    const { boundary, body } = encodeMultipartSafely(
      uploadFields(request),
      { name: FILE_FIELD, fileName: request.meta.fileName, contentType: request.meta.mimeType, bytes: request.bytes },
      random,
    );
    const auth = await authHeaders(endpoint);
    // Signed out: never send evidence without a session. It stays queued until the owner signs in.
    if (endpoint.getAuthToken && !auth.Authorization) return { kind: "auth", status: 401 };
    let response;
    try {
      response = await send({
        method: "POST",
        url: `${endpoint.baseUrl}${UPLOAD_PATH}`,
        headers: {
          Accept: "application/json",
          "Content-Type": `multipart/form-data; boundary=${boundary}`,
          [IDEMPOTENCY_HEADER]: request.idempotencyKey,
          ...auth,
        },
        body,
        timeoutMs: this.options.timeoutMs ?? 120_000,
      });
    } catch {
      return { kind: "network" };
    }
    let text = "";
    try {
      text = await response.text();
    } catch {
      return { kind: "network" };
    }
    if (response.status === 401) endpoint.onUnauthorized?.();
    return classifyResponse(response.status, parseJson(text), response.header("retry-after"), now());
  }
}
