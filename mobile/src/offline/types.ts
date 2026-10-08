/**
 * Offline evidence pipeline types (§43):
 *
 *   capture -> encrypt locally -> queue -> background upload
 *           -> server hash verified -> local copy removed
 *
 * Every platform dependency is a port (interface) so the core is pure and
 * testable. Expo implementations live in ./expo; tests use in-memory fakes.
 */
import type { ISODateTime } from "../lib/dates";

/** Evidence sources produced by the phone. Values match backend `SourceKind` (§7). */
export type CaptureSource = "mobile_scan" | "mobile_share";

/** Evidence formats produced by the phone. Values match backend `EvidenceFormat` (§7). */
export type CaptureFormat = "pdf" | "image" | "screenshot" | "url" | "eml" | "text" | "xml";

/** Quality issues found on the phone. Advisory only; the server decides (§13, §18). */
export type QualityIssue = "blurry" | "glare" | "too_dark";

/** Hints the phone noticed. Never treated as evidence by the server (§3, §18). */
export interface CaptureHints {
  /** Raw QR payloads seen on the page, e.g. a Portuguese invoice QR (§19). */
  qr?: string[];
  quality?: QualityIssue[];
  /** Title the sharing app attached (web page title, email subject). */
  title?: string;
}

export interface CaptureMeta {
  source: CaptureSource;
  format: CaptureFormat;
  fileName: string;
  mimeType: string;
  /** Phone time with offset, e.g. "2026-09-28T10:14:03+01:00". */
  capturedAt: ISODateTime;
  /** Groups the pages of one multi-page scan. */
  captureId?: string;
  /** 1-based page number inside `captureId`. */
  page?: number;
  pageCount?: number;
  /** The link itself, for shared URLs (§9 link intelligence runs on the server). */
  originalUrl?: string;
  hints?: CaptureHints;
}

export interface CaptureInput extends CaptureMeta {
  bytes: Uint8Array;
}

export type CaptureResult =
  | { status: "queued"; id: string }
  | { status: "duplicate"; id: string }
  | { status: "already_sent"; id: string }
  | { status: "rejected"; reason: "empty" | "too_large" };

export type ItemState = "pending" | "uploading" | "verified" | "held";

/** Why the last attempt did not finish. Internal only; never shown raw (§70). */
export type FailureKind = "network" | "server" | "auth" | "hash_mismatch" | "rejected" | "unreadable";

export interface Receipt {
  /** sha256 the server computed over the bytes it stored. */
  sha256: string;
  evidenceId?: string;
  duplicate?: boolean;
  verifiedAt: number;
}

export interface QueueItem {
  /** Random id. Also the Idempotency-Key and the AES-GCM associated data. */
  id: string;
  /** Lower-case hex sha256 of the original bytes. */
  sha256: string;
  byteLength: number;
  meta: CaptureMeta;
  state: ItemState;
  attempts: number;
  mismatches: number;
  /** Epoch ms before which the item is not retried. */
  nextAttemptAt: number;
  /** Epoch ms when an "uploading" claim expires (crash safety). */
  leaseUntil?: number;
  lastFailure?: FailureKind;
  lastStatus?: number;
  receipt?: Receipt;
  createdAt: number;
}

export interface SentRecord {
  id: string;
  sha256: string;
  evidenceId?: string;
  verifiedAt: number;
}

export interface QueueSnapshot {
  version: 1;
  items: QueueItem[];
  /** Most recent verified deliveries, newest first. Used for de-duplication and the UI. */
  sent: SentRecord[];
}

/* ---------- Ports ---------- */

/** Authenticated encryption (AES-256-GCM in production). */
export interface Cipher {
  seal(plaintext: Uint8Array, associatedData: Uint8Array): Promise<Uint8Array>;
  /** Throws if the data was tampered with or the key is wrong. */
  open(sealed: Uint8Array, associatedData: Uint8Array): Promise<Uint8Array>;
}

export interface Hasher {
  /** Lower-case hex sha256. */
  sha256Hex(bytes: Uint8Array): Promise<string>;
}

/** Durable storage for sealed blobs, keyed by item id. Must survive app restarts. */
export interface BlobStore {
  write(key: string, bytes: Uint8Array): Promise<void>;
  read(key: string): Promise<Uint8Array | null>;
  /** Idempotent: deleting a missing key is not an error. */
  delete(key: string): Promise<void>;
  list(): Promise<string[]>;
}

export interface QueueStore {
  load(): Promise<QueueSnapshot | null>;
  save(snapshot: QueueSnapshot): Promise<void>;
}

export interface NetworkMonitor {
  isOnline(): Promise<boolean>;
  /** Returns an unsubscribe function. */
  subscribe(listener: (online: boolean) => void): () => void;
}

export interface UploadRequest {
  idempotencyKey: string;
  sha256: string;
  bytes: Uint8Array;
  meta: CaptureMeta;
}

export type UploadOutcome =
  | { kind: "receipt"; sha256: string; evidenceId?: string; duplicate?: boolean }
  | { kind: "network" }
  | { kind: "retryable"; status?: number; retryAfterMs?: number }
  | { kind: "auth"; status: number }
  | { kind: "mismatch"; status?: number; serverSha256?: string }
  | { kind: "rejected"; status: number };

export interface EvidenceTransport {
  upload(request: UploadRequest): Promise<UploadOutcome>;
}

export interface Clock {
  now(): number;
}
