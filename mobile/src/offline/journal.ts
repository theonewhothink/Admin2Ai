/**
 * Encrypted JSON file for small state at rest (the queue index, cached screens).
 * File names and hashes of owner documents are business data, so they are
 * sealed with the same key as the evidence (§52 encryption at rest).
 */
import { utf8Decode, utf8Encode } from "../lib/bytes";
import { parseCaptureMeta } from "./envelope";
import type { Cipher, FailureKind, ItemState, QueueItem, QueueSnapshot, QueueStore, SentRecord } from "./types";

/** A single file that can be read and atomically replaced. */
export interface RawFile {
  read(): Promise<Uint8Array | null>;
  /** Replace the whole file so a crash leaves either the old or the new content. */
  writeAtomic(bytes: Uint8Array): Promise<void>;
}

export class EncryptedJsonFile<T> {
  constructor(
    private readonly file: RawFile,
    private readonly cipher: Cipher,
    private readonly purpose: string,
    private readonly validate: (value: unknown) => T | null,
  ) {}

  /** Null when missing, undecryptable or invalid. Callers rebuild from durable data. */
  async load(): Promise<T | null> {
    const sealed = await this.file.read();
    if (!sealed) return null;
    try {
      const plain = await this.cipher.open(sealed, utf8Encode(this.purpose));
      return this.validate(JSON.parse(utf8Decode(plain)) as unknown);
    } catch {
      return null;
    }
  }

  async save(value: T): Promise<void> {
    const sealed = await this.cipher.seal(utf8Encode(JSON.stringify(value)), utf8Encode(this.purpose));
    await this.file.writeAtomic(sealed);
  }
}

const STATES: readonly ItemState[] = ["pending", "uploading", "verified", "held"];
const FAILURES: readonly FailureKind[] = ["network", "server", "auth", "hash_mismatch", "rejected", "unreadable"];

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isCount(v: unknown): v is number {
  return typeof v === "number" && Number.isInteger(v) && v >= 0;
}

function parseItem(value: unknown): QueueItem | null {
  if (!isRecord(value)) return null;
  const meta = parseCaptureMeta(value.meta);
  if (!meta || typeof value.id !== "string" || typeof value.sha256 !== "string") return null;
  if (!STATES.includes(value.state as ItemState)) return null;
  if (!isCount(value.byteLength) || !isCount(value.attempts) || !isCount(value.mismatches)) return null;
  if (typeof value.nextAttemptAt !== "number" || typeof value.createdAt !== "number") return null;
  if (value.lastFailure !== undefined && !FAILURES.includes(value.lastFailure as FailureKind)) return null;
  return { ...(value as unknown as QueueItem), meta };
}

function parseSent(value: unknown): SentRecord | null {
  if (!isRecord(value)) return null;
  if (typeof value.id !== "string" || typeof value.sha256 !== "string" || typeof value.verifiedAt !== "number") {
    return null;
  }
  return value as unknown as SentRecord;
}

/**
 * Validate a snapshot read from disk. Malformed entries are dropped one by one
 * rather than discarding the whole queue; their blobs are re-registered by recovery.
 */
export function parseSnapshot(value: unknown): QueueSnapshot | null {
  if (!isRecord(value) || value.version !== 1 || !Array.isArray(value.items) || !Array.isArray(value.sent)) {
    return null;
  }
  return {
    version: 1,
    items: value.items.map(parseItem).filter((i): i is QueueItem => i !== null),
    sent: value.sent.map(parseSent).filter((s): s is SentRecord => s !== null),
  };
}

export class EncryptedQueueStore implements QueueStore {
  private readonly store: EncryptedJsonFile<QueueSnapshot>;

  constructor(file: RawFile, cipher: Cipher) {
    this.store = new EncryptedJsonFile(file, cipher, "backoffice.queue.v1", parseSnapshot);
  }

  load(): Promise<QueueSnapshot | null> {
    return this.store.load();
  }

  save(snapshot: QueueSnapshot): Promise<void> {
    return this.store.save(snapshot);
  }
}
