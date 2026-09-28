/**
 * Offline evidence pipeline (§43):
 *
 *   capture -> encrypt locally -> queue -> background upload
 *           -> server hash verified -> local copy removed
 *
 * Guarantees, each covered by tests:
 * - The local copy is deleted only after a receipt whose server-computed sha256
 *   equals the phone's sha256, and only after that receipt is durably recorded.
 * - A mismatching hash, a rejected upload or an unreadable local copy never
 *   deletes anything (§3: nothing closes without evidence).
 * - Re-uploads are idempotent: the queue item id is sent as Idempotency-Key and
 *   the server de-duplicates by hash, so a lost response is simply retried.
 * - Captures survive crashes: metadata lives inside the sealed blob, so blobs
 *   written before the queue index are re-registered by `recover()`.
 */
import { normalizeHexDigest, utf8Encode } from "../lib/bytes";
import { DEFAULT_BACKOFF, retryDelay, type BackoffPolicy } from "./backoff";
import { decodeEnvelope, encodeEnvelope } from "./envelope";
import { Mutex } from "./mutex";
import type {
  BlobStore,
  CaptureInput,
  CaptureMeta,
  CaptureResult,
  Cipher,
  Clock,
  EvidenceTransport,
  FailureKind,
  Hasher,
  NetworkMonitor,
  QueueItem,
  QueueSnapshot,
  QueueStore,
  SentRecord,
  UploadOutcome,
} from "./types";

export interface PipelineDeps {
  cipher: Cipher;
  hasher: Hasher;
  blobs: BlobStore;
  queue: QueueStore;
  network: NetworkMonitor;
  transport: EvidenceTransport;
  clock: Clock;
  /** Returns a number in [0, 1). */
  random: () => number;
  /** Unique, unguessable id (UUID v4 in production). */
  newId: () => string;
}

export interface PipelineConfig {
  /** Largest single file accepted from the phone. */
  maxBytes: number;
  /** How long an "uploading" claim lasts; must exceed the upload timeout. */
  leaseMs: number;
  /** Consecutive hash mismatches before the item is held for attention. */
  maxMismatches: number;
  /** How many verified deliveries to remember for de-duplication. */
  sentHistory: number;
  backoff: BackoffPolicy;
}

export const DEFAULT_PIPELINE_CONFIG: PipelineConfig = {
  maxBytes: 40 * 1024 * 1024,
  leaseMs: 5 * 60_000,
  maxMismatches: 5,
  sentHistory: 200,
  backoff: DEFAULT_BACKOFF,
};

export interface DrainReport {
  offline: boolean;
  attempted: number;
  verified: number;
  retrying: number;
  held: number;
  /** True when the deadline stopped the drain with work still due. */
  stoppedEarly: boolean;
}

export interface RecoveryReport {
  /** Verified items whose local copy is now deleted. */
  finalized: number;
  /** Expired "uploading" claims put back in the queue. */
  released: number;
  /** Blobs found without a queue entry and queued again. */
  reRegistered: number;
  /** Blobs already delivered (receipt on record) and now deleted. */
  alreadySent: number;
  /** Blobs that duplicate a queued item's content, removed. */
  redundant: number;
  /** Blobs that could not be decrypted. Kept, never deleted. */
  unreadable: number;
  /** Queue entries whose local copy is missing. Held for attention. */
  missing: number;
}

export interface HeldItem {
  id: string;
  fileName: string;
  format: CaptureMeta["format"];
  reason: FailureKind;
}

export interface QueueSummary {
  /** Items not yet confirmed by the server. */
  waiting: number;
  sending: boolean;
  held: HeldItem[];
  /** Verified deliveries, newest first. */
  sent: SentRecord[];
  /** Earliest time a waiting item is due, for scheduling a foreground retry. */
  nextDueAt: number | null;
}

type AttemptResult = "verified" | "retrying" | "held" | "network";

const EMPTY: () => QueueSnapshot = () => ({ version: 1, items: [], sent: [] });

function pickMeta(input: CaptureInput): CaptureMeta {
  const meta: CaptureMeta = {
    source: input.source,
    format: input.format,
    fileName: input.fileName,
    mimeType: input.mimeType,
    capturedAt: input.capturedAt,
  };
  if (input.captureId !== undefined) meta.captureId = input.captureId;
  if (input.page !== undefined) meta.page = input.page;
  if (input.pageCount !== undefined) meta.pageCount = input.pageCount;
  if (input.originalUrl !== undefined) meta.originalUrl = input.originalUrl;
  if (input.hints !== undefined) meta.hints = input.hints;
  return meta;
}

export class OfflinePipeline {
  private readonly mutex = new Mutex();
  private readonly config: PipelineConfig;
  private readonly listeners = new Set<(summary: QueueSummary) => void>();

  constructor(
    private readonly deps: PipelineDeps,
    config: Partial<PipelineConfig> = {},
  ) {
    this.config = { ...DEFAULT_PIPELINE_CONFIG, ...config };
  }

  /* ---------- Public API ---------- */

  /** Hash, seal and queue one file. The plaintext is not kept by the pipeline. */
  async capture(input: CaptureInput): Promise<CaptureResult> {
    if (input.bytes.length === 0) return { status: "rejected", reason: "empty" };
    if (input.bytes.length > this.config.maxBytes) return { status: "rejected", reason: "too_large" };
    const sha256 = await this.hash(input.bytes);
    const meta = pickMeta(input);
    return this.mutex.run(async () => {
      const snap = await this.load();
      const queued = snap.items.find((i) => i.sha256 === sha256);
      if (queued) return { status: "duplicate", id: queued.id };
      const sent = snap.sent.find((s) => s.sha256 === sha256);
      if (sent) return { status: "already_sent", id: sent.id };

      const id = this.deps.newId();
      const sealed = await this.deps.cipher.seal(encodeEnvelope(meta, input.bytes), utf8Encode(id));
      // Blob first, index second: a crash in between leaves an orphan that recover() re-registers.
      await this.deps.blobs.write(id, sealed);
      const now = this.now();
      snap.items.push({
        id,
        sha256,
        byteLength: input.bytes.length,
        meta,
        state: "pending",
        attempts: 0,
        mismatches: 0,
        nextAttemptAt: now,
        createdAt: now,
      });
      await this.persist(snap);
      return { status: "queued", id };
    });
  }

  /**
   * Upload every due item, oldest first, until the queue is empty, the network
   * drops, or `deadline` (epoch ms) passes. Safe to call often; calls are serialised.
   */
  drain(options: { deadline?: number } = {}): Promise<DrainReport> {
    return this.mutex.run(async () => {
      const report: DrainReport = { offline: false, attempted: 0, verified: 0, retrying: 0, held: 0, stoppedEarly: false };
      const snap = await this.load();
      await this.finalizeAll(snap);
      if (!(await this.isOnline())) {
        report.offline = true;
        return report;
      }
      const tried = new Set<string>();
      for (;;) {
        const item = this.nextDue(snap, tried);
        if (!item) break;
        if (options.deadline !== undefined && this.now() >= options.deadline) {
          report.stoppedEarly = true;
          break;
        }
        tried.add(item.id);
        const result = await this.attempt(snap, item);
        report.attempted += 1;
        if (result === "verified") report.verified += 1;
        else if (result === "held") report.held += 1;
        else report.retrying += 1;
        // The connection dropped: stop here and wait for the network listener.
        if (result === "network") break;
      }
      return report;
    });
  }

  /** Run once at start-up (and in the background task) to repair any interrupted work. */
  recover(): Promise<RecoveryReport> {
    return this.mutex.run(async () => {
      const report: RecoveryReport = {
        finalized: 0, released: 0, reRegistered: 0, alreadySent: 0, redundant: 0, unreadable: 0, missing: 0,
      };
      const snap = await this.load();
      report.finalized = await this.finalizeAll(snap);
      const now = this.now();
      for (const item of snap.items) {
        if (item.state === "uploading" && (item.leaseUntil ?? 0) <= now) {
          item.state = "pending";
          delete item.leaseUntil;
          item.nextAttemptAt = now;
          report.released += 1;
        }
      }
      const keys = await this.deps.blobs.list();
      const known = new Set(snap.items.map((i) => i.id));
      for (const key of keys) {
        if (!known.has(key)) await this.adoptOrphan(snap, key, report);
      }
      const present = new Set(keys);
      for (const item of snap.items) {
        if ((item.state === "pending" || item.state === "uploading") && !present.has(item.id)) {
          this.hold(item, "unreadable");
          report.missing += 1;
        }
      }
      await this.persist(snap);
      return report;
    });
  }

  summary(): Promise<QueueSummary> {
    return this.mutex.run(async () => this.summarize(await this.load()));
  }

  /** Owner asked to try a held item again. */
  retryHeld(id: string): Promise<boolean> {
    return this.mutex.run(async () => {
      const snap = await this.load();
      const item = snap.items.find((i) => i.id === id && i.state === "held");
      if (!item) return false;
      item.state = "pending";
      item.mismatches = 0;
      item.nextAttemptAt = this.now();
      delete item.lastFailure;
      await this.persist(snap);
      return true;
    });
  }

  /**
   * Owner explicitly removed a held item that never reached the server.
   * Only held items qualify; anything still in flight is never discarded.
   */
  discardHeld(id: string): Promise<boolean> {
    return this.mutex.run(async () => {
      const snap = await this.load();
      const item = snap.items.find((i) => i.id === id && i.state === "held");
      if (!item) return false;
      await this.deps.blobs.delete(id);
      snap.items = snap.items.filter((i) => i.id !== id);
      await this.persist(snap);
      return true;
    });
  }

  subscribe(listener: (summary: QueueSummary) => void): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /* ---------- Upload ---------- */

  private async attempt(snap: QueueSnapshot, item: QueueItem): Promise<AttemptResult> {
    item.state = "uploading";
    item.leaseUntil = this.now() + this.config.leaseMs;
    await this.persist(snap);

    const bytes = await this.readLocal(item);
    if (!bytes) {
      this.hold(item, "unreadable");
      await this.persist(snap);
      return "held";
    }

    let outcome: UploadOutcome;
    try {
      outcome = await this.deps.transport.upload({ idempotencyKey: item.id, sha256: item.sha256, bytes, meta: item.meta });
    } catch {
      outcome = { kind: "network" };
    }
    item.attempts += 1;
    return this.settle(snap, item, outcome);
  }

  private async settle(snap: QueueSnapshot, item: QueueItem, outcome: UploadOutcome): Promise<AttemptResult> {
    switch (outcome.kind) {
      case "receipt": {
        const serverSha = normalizeHexDigest(outcome.sha256);
        if (serverSha === null || serverSha !== item.sha256) return this.mismatch(snap, item);
        item.state = "verified";
        delete item.leaseUntil;
        delete item.lastFailure;
        item.receipt = { sha256: serverSha, verifiedAt: this.now() };
        if (outcome.evidenceId) item.receipt.evidenceId = outcome.evidenceId;
        if (outcome.duplicate !== undefined) item.receipt.duplicate = outcome.duplicate;
        // Record the verified receipt durably before touching the local copy.
        await this.persist(snap);
        await this.finalize(snap, item);
        return "verified";
      }
      case "mismatch":
        return this.mismatch(snap, item, outcome.status);
      case "network":
        await this.reschedule(snap, item, "network");
        return "network";
      case "retryable":
        await this.reschedule(snap, item, "server", outcome.status, outcome.retryAfterMs);
        return "retrying";
      case "auth":
        await this.reschedule(snap, item, "auth", outcome.status);
        return "retrying";
      case "rejected":
        this.hold(item, "rejected", outcome.status);
        await this.persist(snap);
        return "held";
    }
  }

  /** The server stored different bytes (or said so). Keep the local copy and try again. */
  private async mismatch(snap: QueueSnapshot, item: QueueItem, status?: number): Promise<AttemptResult> {
    item.mismatches += 1;
    if (item.mismatches >= this.config.maxMismatches) {
      this.hold(item, "hash_mismatch", status);
      await this.persist(snap);
      return "held";
    }
    await this.reschedule(snap, item, "hash_mismatch", status);
    return "retrying";
  }

  private async reschedule(
    snap: QueueSnapshot,
    item: QueueItem,
    failure: FailureKind,
    status?: number,
    retryAfterMs?: number,
  ): Promise<void> {
    item.state = "pending";
    delete item.leaseUntil;
    item.lastFailure = failure;
    if (status !== undefined) item.lastStatus = status;
    item.nextAttemptAt = this.now() + retryDelay(item.attempts, this.config.backoff, this.deps.random, retryAfterMs);
    await this.persist(snap);
  }

  private hold(item: QueueItem, failure: FailureKind, status?: number): void {
    item.state = "held";
    delete item.leaseUntil;
    item.lastFailure = failure;
    if (status !== undefined) item.lastStatus = status;
  }

  /** Decrypt and re-hash the local copy. Null if missing, tampered or changed. */
  private async readLocal(item: QueueItem): Promise<Uint8Array | null> {
    try {
      const sealed = await this.deps.blobs.read(item.id);
      if (!sealed) return null;
      const { bytes } = decodeEnvelope(await this.deps.cipher.open(sealed, utf8Encode(item.id)));
      return (await this.hash(bytes)) === item.sha256 ? bytes : null;
    } catch {
      return null;
    }
  }

  /* ---------- Deletion (only after a verified receipt) ---------- */

  private async finalize(snap: QueueSnapshot, item: QueueItem): Promise<boolean> {
    const receipt = item.receipt;
    if (item.state !== "verified" || !receipt || receipt.sha256 !== item.sha256) return false;
    try {
      await this.deps.blobs.delete(item.id);
    } catch {
      return false; // Still "verified"; the next drain or recovery retries the deletion.
    }
    snap.items = snap.items.filter((i) => i.id !== item.id);
    const record: SentRecord = { id: item.id, sha256: item.sha256, verifiedAt: receipt.verifiedAt };
    if (receipt.evidenceId) record.evidenceId = receipt.evidenceId;
    snap.sent = [record, ...snap.sent.filter((s) => s.sha256 !== item.sha256)].slice(0, this.config.sentHistory);
    await this.persist(snap);
    return true;
  }

  private async finalizeAll(snap: QueueSnapshot): Promise<number> {
    let count = 0;
    for (const item of snap.items.filter((i) => i.state === "verified")) {
      if (await this.finalize(snap, item)) count += 1;
    }
    return count;
  }

  /* ---------- Recovery ---------- */

  private async adoptOrphan(snap: QueueSnapshot, key: string, report: RecoveryReport): Promise<void> {
    let restored: { meta: CaptureMeta; bytes: Uint8Array } | null = null;
    try {
      const sealed = await this.deps.blobs.read(key);
      if (sealed) restored = decodeEnvelope(await this.deps.cipher.open(sealed, utf8Encode(key)));
    } catch {
      restored = null;
    }
    if (!restored) {
      report.unreadable += 1; // Kept on disk: never delete what we cannot read.
      return;
    }
    const sha256 = await this.hash(restored.bytes);
    if (snap.sent.some((s) => s.sha256 === sha256)) {
      // The server already confirmed these exact bytes.
      await this.deps.blobs.delete(key);
      report.alreadySent += 1;
      return;
    }
    if (snap.items.some((i) => i.sha256 === sha256)) {
      // The same bytes are already queued under another item; this copy is redundant.
      await this.deps.blobs.delete(key);
      report.redundant += 1;
      return;
    }
    const now = this.now();
    snap.items.push({
      id: key,
      sha256,
      byteLength: restored.bytes.length,
      meta: restored.meta,
      state: "pending",
      attempts: 0,
      mismatches: 0,
      nextAttemptAt: now,
      createdAt: now,
    });
    report.reRegistered += 1;
  }

  /* ---------- Helpers ---------- */

  private nextDue(snap: QueueSnapshot, tried: ReadonlySet<string>): QueueItem | undefined {
    const now = this.now();
    return snap.items
      .filter((i) => !tried.has(i.id) && this.isDue(i, now))
      .sort((a, b) => a.createdAt - b.createdAt || (a.meta.page ?? 0) - (b.meta.page ?? 0))[0];
  }

  private isDue(item: QueueItem, now: number): boolean {
    if (item.state === "pending") return item.nextAttemptAt <= now;
    // An expired claim means the uploading process died mid-flight.
    if (item.state === "uploading") return (item.leaseUntil ?? 0) <= now;
    return false;
  }

  private summarize(snap: QueueSnapshot): QueueSummary {
    const now = this.now();
    // "Verified" items already reached the server; only their local deletion is pending.
    const waiting = snap.items.filter((i) => i.state === "pending" || i.state === "uploading");
    const due = waiting.filter((i) => i.state === "pending").map((i) => i.nextAttemptAt);
    return {
      waiting: waiting.length,
      sending: waiting.some((i) => i.state === "uploading" && (i.leaseUntil ?? 0) > now),
      held: snap.items
        .filter((i) => i.state === "held")
        .map((i) => ({ id: i.id, fileName: i.meta.fileName, format: i.meta.format, reason: i.lastFailure ?? "rejected" })),
      sent: snap.sent.slice(),
      nextDueAt: due.length > 0 ? Math.min(...due) : null,
    };
  }

  private async hash(bytes: Uint8Array): Promise<string> {
    const hex = normalizeHexDigest(await this.deps.hasher.sha256Hex(bytes));
    if (!hex) throw new Error("hasher returned an invalid sha256");
    return hex;
  }

  private async isOnline(): Promise<boolean> {
    try {
      return await this.deps.network.isOnline();
    } catch {
      return false;
    }
  }

  /** Always re-read from disk: another JS runtime (background task) may have written. */
  private async load(): Promise<QueueSnapshot> {
    return (await this.deps.queue.load()) ?? EMPTY();
  }

  private async persist(snap: QueueSnapshot): Promise<void> {
    await this.deps.queue.save(snap);
    const summary = this.summarize(snap);
    for (const listener of this.listeners) {
      try {
        listener(summary);
      } catch {
        // A UI listener must never break the queue.
      }
    }
  }

  private now(): number {
    return this.deps.clock.now();
  }
}
