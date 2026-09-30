import { describe, expect, it } from "@jest/globals";
import contract from "../../../contracts/evidence-upload.json";
import { backoffDelay, DEFAULT_BACKOFF, parseRetryAfter, retryDelay, MAX_RETRY_AFTER_MS } from "../backoff";
import {
  AUTH_STATUSES,
  FILE_FIELD,
  FORMATS,
  HASH_MISMATCH_ERROR,
  IDEMPOTENCY_HEADER,
  REQUIRED_FIELDS,
  RETRYABLE_STATUSES,
  SOURCE_KINDS,
  UPLOAD_PATH,
} from "../contract";
import { decodeEnvelope, encodeEnvelope, frameSealed, unframeSealed } from "../envelope";
import { EncryptedQueueStore, parseSnapshot, type RawFile } from "../journal";
import { Mutex } from "../mutex";
import type { CaptureMeta, QueueSnapshot } from "../types";
import { NodeCipher } from "./fakes";

const META: CaptureMeta = {
  source: "mobile_scan",
  format: "image",
  fileName: "página 1.jpg",
  mimeType: "image/jpeg",
  capturedAt: "2026-09-28T10:00:00+01:00",
  captureId: "cap",
  page: 1,
  pageCount: 1,
};

class MemoryFile implements RawFile {
  bytes: Uint8Array | null = null;
  async read() {
    return this.bytes;
  }
  async writeAtomic(bytes: Uint8Array) {
    this.bytes = bytes.slice();
  }
}

describe("backoff", () => {
  it("grows exponentially with bounded equal jitter", () => {
    const zero = () => 0;
    const one = () => 0.999999;
    expect(backoffDelay(1, DEFAULT_BACKOFF, zero)).toBe(2_500);
    expect(backoffDelay(1, DEFAULT_BACKOFF, one)).toBe(5_000);
    expect(backoffDelay(4, DEFAULT_BACKOFF, zero)).toBe(20_000);
    const capped = backoffDelay(50, DEFAULT_BACKOFF, one);
    expect(capped).toBeLessThanOrEqual(DEFAULT_BACKOFF.maxMs);
    expect(capped).toBeGreaterThanOrEqual(DEFAULT_BACKOFF.maxMs - 1);
    expect(backoffDelay(0, DEFAULT_BACKOFF, zero)).toBe(2_500);
  });

  it("uses the server's Retry-After only when it is longer, and bounds it", () => {
    expect(retryDelay(1, DEFAULT_BACKOFF, () => 0, 1_000)).toBe(2_500);
    expect(retryDelay(1, DEFAULT_BACKOFF, () => 0, 60_000)).toBe(60_000);
    expect(retryDelay(1, DEFAULT_BACKOFF, () => 0, 10 * MAX_RETRY_AFTER_MS)).toBe(MAX_RETRY_AFTER_MS);
    expect(retryDelay(1, DEFAULT_BACKOFF, () => 0, Number.NaN)).toBe(2_500);
  });

  it("parses Retry-After seconds and HTTP dates", () => {
    const now = Date.UTC(2026, 8, 28, 9, 0, 0);
    expect(parseRetryAfter("120", now)).toBe(120_000);
    expect(parseRetryAfter(new Date(now + 5_000).toUTCString(), now)).toBe(5_000);
    expect(parseRetryAfter(new Date(now - 5_000).toUTCString(), now)).toBe(0);
    expect(parseRetryAfter("soon", now)).toBeUndefined();
    expect(parseRetryAfter(null, now)).toBeUndefined();
  });
});

describe("envelope and sealed frame", () => {
  it("round-trips metadata and bytes", () => {
    const bytes = Uint8Array.from([0, 1, 2, 255]);
    const out = decodeEnvelope(encodeEnvelope(META, bytes));
    expect(out.meta).toEqual(META);
    expect(Array.from(out.bytes)).toEqual([0, 1, 2, 255]);
  });

  it("rejects truncated or foreign data", () => {
    const env = encodeEnvelope(META, Uint8Array.of(1));
    expect(() => decodeEnvelope(env.slice(0, 6))).toThrow();
    expect(() => decodeEnvelope(Uint8Array.from([1, 2, 3, 4, 0, 0, 0, 0]))).toThrow("magic");
    const badMeta = encodeEnvelope({ ...META, source: "email" as never }, Uint8Array.of(1));
    expect(() => decodeEnvelope(badMeta)).toThrow("meta invalid");
  });

  it("frames sealed data with a version byte and refuses unknown versions", () => {
    const framed = frameSealed(new Uint8Array(12), new Uint8Array(20));
    expect(framed[0]).toBe(1);
    expect(unframeSealed(framed).ciphertextWithTag).toHaveLength(20);
    framed[0] = 2;
    expect(() => unframeSealed(framed)).toThrow("unsupported sealed version 2");
    expect(() => frameSealed(new Uint8Array(8), new Uint8Array(20))).toThrow();
  });
});

describe("encrypted queue journal", () => {
  const snapshot: QueueSnapshot = {
    version: 1,
    items: [
      {
        id: "i1", sha256: "a".repeat(64), byteLength: 3, meta: META, state: "pending",
        attempts: 0, mismatches: 0, nextAttemptAt: 1, createdAt: 1,
      },
    ],
    sent: [{ id: "i0", sha256: "b".repeat(64), verifiedAt: 1 }],
  };

  it("stores the queue encrypted and reads it back", async () => {
    const file = new MemoryFile();
    const store = new EncryptedQueueStore(file, new NodeCipher());
    await store.save(snapshot);
    expect(Buffer.from(file.bytes!).includes(Buffer.from("página"))).toBe(false);
    expect(await store.load()).toEqual(snapshot);
  });

  it("returns null for a missing, foreign-key or corrupt journal", async () => {
    const file = new MemoryFile();
    expect(await new EncryptedQueueStore(file, new NodeCipher()).load()).toBeNull();
    await new EncryptedQueueStore(file, new NodeCipher(new Uint8Array(32).fill(1))).save(snapshot);
    expect(await new EncryptedQueueStore(file, new NodeCipher()).load()).toBeNull();
  });

  it("drops malformed entries one by one", () => {
    const parsed = parseSnapshot({
      version: 1,
      items: [snapshot.items[0], { id: "broken" }, { ...snapshot.items[0], state: "exploded" }],
      sent: [snapshot.sent[0], { id: 3 }],
    });
    expect(parsed?.items.map((i) => i.id)).toEqual(["i1"]);
    expect(parsed?.sent).toHaveLength(1);
    expect(parseSnapshot({ version: 2, items: [], sent: [] })).toBeNull();
  });
});

describe("mutex", () => {
  it("runs tasks one at a time, in order, surviving failures", async () => {
    const m = new Mutex();
    const log: string[] = [];
    const slow = (name: string, ms: number) =>
      m.run(async () => {
        log.push(`start ${name}`);
        await new Promise((r) => setTimeout(r, ms));
        log.push(`end ${name}`);
      });
    const failing = m.run(async () => {
      throw new Error("x");
    });
    await Promise.all([slow("a", 5), failing.catch(() => undefined), slow("b", 1)]);
    expect(log).toEqual(["start a", "end a", "start b", "end b"]);
  });
});

describe("upload contract", () => {
  it("TypeScript constants match contracts/evidence-upload.json", () => {
    expect(UPLOAD_PATH).toBe(contract.endpoint);
    expect(IDEMPOTENCY_HEADER).toBe(contract.idempotency_header);
    expect(FILE_FIELD).toBe(contract.file_field);
    expect(HASH_MISMATCH_ERROR).toBe(contract.hash_mismatch_error);
    expect([...REQUIRED_FIELDS]).toEqual(contract.required_fields);
    expect([...SOURCE_KINDS]).toEqual(contract.source_kinds);
    expect([...FORMATS]).toEqual(contract.formats);
    expect([...RETRYABLE_STATUSES]).toEqual(contract.retryable_statuses);
    expect([...AUTH_STATUSES]).toEqual(contract.auth_statuses);
    for (const f of REQUIRED_FIELDS) expect(Object.keys(contract.fields)).toContain(f);
  });
});
