/**
 * The evidence upload contract (§43), mirrored from contracts/evidence-upload.json.
 * A unit test keeps the two in sync; the backend test checks the vocabularies
 * against `SourceKind` and `EvidenceFormat`.
 */
import type { CaptureFormat, CaptureSource } from "./types";

export const UPLOAD_PATH = "/api/evidence/upload";
export const IDEMPOTENCY_HEADER = "Idempotency-Key";
export const FILE_FIELD = "file";
export const HASH_MISMATCH_ERROR = "hash_mismatch";

export const REQUIRED_FIELDS = ["sha256", "source", "format", "captured_at", "client_item_id"] as const;

export const SOURCE_KINDS: readonly CaptureSource[] = ["mobile_scan", "mobile_share"];
export const FORMATS: readonly CaptureFormat[] = ["pdf", "image", "screenshot", "url", "eml", "text", "xml"];

export const RETRYABLE_STATUSES: readonly number[] = [408, 425, 429, 500, 502, 503, 504];
export const AUTH_STATUSES: readonly number[] = [401, 403];
