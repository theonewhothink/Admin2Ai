/**
 * Test doubles for the offline pipeline ports. The cipher and hasher are real
 * (Node WebCrypto AES-256-GCM and sha256) and use the production sealed frame,
 * so tests exercise genuine encryption, tamper detection and hashing.
 */
import { createHash, webcrypto } from "node:crypto";
import { ownedBytes, toHex } from "../../lib/bytes";
import { frameSealed, unframeSealed, GCM_NONCE_BYTES } from "../envelope";
import type {
  BlobStore,
  Cipher,
  Clock,
  EvidenceTransport,
  Hasher,
  NetworkMonitor,
  QueueSnapshot,
  QueueStore,
  UploadOutcome,
  UploadRequest,
} from "../types";
import type { PipelineDeps } from "../pipeline";

export class NodeCipher implements Cipher {
  private keyPromise: Promise<webcrypto.CryptoKey>;

  constructor(rawKey: Uint8Array = new Uint8Array(32).fill(7)) {
    this.keyPromise = webcrypto.subtle.importKey("raw", ownedBytes(rawKey), "AES-GCM", false, ["encrypt", "decrypt"]);
  }

  async seal(plaintext: Uint8Array, associatedData: Uint8Array): Promise<Uint8Array> {
    const nonce = webcrypto.getRandomValues(new Uint8Array(GCM_NONCE_BYTES));
    const ct = await webcrypto.subtle.encrypt(
      { name: "AES-GCM", iv: nonce, additionalData: ownedBytes(associatedData), tagLength: 128 },
      await this.keyPromise,
      ownedBytes(plaintext),
    );
    return frameSealed(nonce, new Uint8Array(ct));
  }

  async open(sealed: Uint8Array, associatedData: Uint8Array): Promise<Uint8Array> {
    const { nonce, ciphertextWithTag } = unframeSealed(sealed);
    const pt = await webcrypto.subtle.decrypt(
      { name: "AES-GCM", iv: ownedBytes(nonce), additionalData: ownedBytes(associatedData), tagLength: 128 },
      await this.keyPromise,
      ownedBytes(ciphertextWithTag),
    );
    return new Uint8Array(pt);
  }
}

export const nodeHasher: Hasher = {
  async sha256Hex(bytes) {
    return createHash("sha256").update(bytes).digest("hex");
  },
};

export function sha256(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

export type Op = { op: "write" | "delete"; key: string };

export class MemoryBlobStore implements BlobStore {
  readonly blobs = new Map<string, Uint8Array>();
  readonly log: Op[] = [];
  failDeletes = false;

  async write(key: string, bytes: Uint8Array): Promise<void> {
    this.log.push({ op: "write", key });
    this.blobs.set(key, bytes.slice());
  }
  async read(key: string): Promise<Uint8Array | null> {
    return this.blobs.get(key)?.slice() ?? null;
  }
  async delete(key: string): Promise<void> {
    if (this.failDeletes) throw new Error("disk busy");
    this.log.push({ op: "delete", key });
    this.blobs.delete(key);
  }
  async list(): Promise<string[]> {
    return [...this.blobs.keys()];
  }
}

/** Persists a deep copy, like a real file would. */
export class MemoryQueueStore implements QueueStore {
  saved: string | null = null;
  saves = 0;
  failNextSave = false;

  async load(): Promise<QueueSnapshot | null> {
    return this.saved ? (JSON.parse(this.saved) as QueueSnapshot) : null;
  }
  async save(snapshot: QueueSnapshot): Promise<void> {
    if (this.failNextSave) {
      this.failNextSave = false;
      throw new Error("disk full");
    }
    this.saves += 1;
    this.saved = JSON.stringify(snapshot);
  }
  snapshot(): QueueSnapshot {
    if (!this.saved) throw new Error("nothing saved");
    return JSON.parse(this.saved) as QueueSnapshot;
  }
}

export class FakeNetwork implements NetworkMonitor {
  private listeners = new Set<(online: boolean) => void>();
  constructor(public online = true) {}
  async isOnline(): Promise<boolean> {
    return this.online;
  }
  subscribe(listener: (online: boolean) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
  set(online: boolean): void {
    this.online = online;
    for (const l of this.listeners) l(online);
  }
}

export class FakeClock implements Clock {
  constructor(public t = Date.UTC(2026, 8, 28, 9, 0, 0)) {}
  now(): number {
    return this.t;
  }
  advance(ms: number): void {
    this.t += ms;
  }
}

/**
 * An idempotent evidence server: stores bytes by sha256 and returns the hash of
 * what it stored. `corrupt` flips a byte on arrival to simulate damage in transit.
 */
export class FakeServer implements EvidenceTransport {
  readonly stored = new Map<string, Uint8Array>();
  readonly requests: UploadRequest[] = [];
  /** Outcomes to return before normal handling, one per call. */
  script: Array<UploadOutcome | "store-then-drop" | "corrupt" | "throw"> = [];

  async upload(request: UploadRequest): Promise<UploadOutcome> {
    this.requests.push(request);
    const step = this.script.shift();
    if (step === "throw") throw new Error("socket closed");
    if (step && typeof step === "object") return step;
    let bytes = request.bytes.slice();
    if (step === "corrupt" && bytes.length > 0) bytes[0] = (bytes[0]! ^ 0xff) & 0xff;
    const hash = sha256(bytes);
    const duplicate = this.stored.has(hash);
    this.stored.set(hash, bytes);
    // Stored, but the response never reaches the phone.
    if (step === "store-then-drop") return { kind: "network" };
    return { kind: "receipt", sha256: hash, evidenceId: `ev_${hash.slice(0, 8)}`, duplicate };
  }
}

export interface Harness {
  deps: PipelineDeps;
  blobs: MemoryBlobStore;
  queue: MemoryQueueStore;
  network: FakeNetwork;
  server: FakeServer;
  clock: FakeClock;
  cipher: NodeCipher;
}

export function harness(overrides: Partial<PipelineDeps> = {}): Harness {
  const blobs = new MemoryBlobStore();
  const queue = new MemoryQueueStore();
  const network = new FakeNetwork(true);
  const server = new FakeServer();
  const clock = new FakeClock();
  const cipher = new NodeCipher();
  let n = 0;
  const deps: PipelineDeps = {
    cipher,
    hasher: nodeHasher,
    blobs,
    queue,
    network,
    transport: server,
    clock,
    random: () => 0.5,
    newId: () => `item-${String(++n).padStart(3, "0")}`,
    ...overrides,
  };
  return { deps, blobs, queue, network, server, clock, cipher };
}

export function bytesOf(text: string): Uint8Array {
  return new TextEncoder().encode(text);
}

export function hex(bytes: Uint8Array): string {
  return toHex(bytes);
}
