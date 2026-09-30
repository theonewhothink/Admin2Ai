/**
 * Last-known screen data, so the phone can show what it knew when offline
 * instead of example data. Stored sealed with the evidence key (§52).
 */
import { EncryptedJsonFile, type RawFile } from "../offline/journal";
import type { Cipher } from "../offline/types";
import { isRecord } from "./http";

export interface CachedEntry {
  savedAt: number;
  data: unknown;
}

export interface SnapshotCache {
  get(key: string): Promise<CachedEntry | null>;
  set(key: string, data: unknown, savedAt: number): Promise<void>;
  /** Forget every screen (sign-out). */
  clear(): Promise<void>;
}

export class MemorySnapshotCache implements SnapshotCache {
  private readonly entries = new Map<string, CachedEntry>();

  async get(key: string): Promise<CachedEntry | null> {
    return this.entries.get(key) ?? null;
  }

  async set(key: string, data: unknown, savedAt: number): Promise<void> {
    this.entries.set(key, { savedAt, data });
  }

  async clear(): Promise<void> {
    this.entries.clear();
  }
}

function parseEntries(value: unknown): Record<string, CachedEntry> | null {
  if (!isRecord(value)) return null;
  const out: Record<string, CachedEntry> = {};
  for (const [key, entry] of Object.entries(value)) {
    if (isRecord(entry) && typeof entry.savedAt === "number") out[key] = { savedAt: entry.savedAt, data: entry.data };
  }
  return out;
}

/** One sealed file holding every cached screen. Failures degrade to "no cache". */
export class SealedSnapshotCache implements SnapshotCache {
  private readonly file: EncryptedJsonFile<Record<string, CachedEntry>>;
  private memory: Record<string, CachedEntry> | null = null;

  constructor(raw: RawFile, cipher: Cipher) {
    this.file = new EncryptedJsonFile(raw, cipher, "backoffice.screens.v1", parseEntries);
  }

  async get(key: string): Promise<CachedEntry | null> {
    return (await this.load())[key] ?? null;
  }

  async set(key: string, data: unknown, savedAt: number): Promise<void> {
    const entries = { ...(await this.load()), [key]: { savedAt, data } };
    this.memory = entries;
    try {
      await this.file.save(entries);
    } catch {
      // Caching is a convenience; the screen already has fresh data.
    }
  }

  async clear(): Promise<void> {
    this.memory = {};
    await this.file.save({});
  }

  private async load(): Promise<Record<string, CachedEntry>> {
    if (!this.memory) {
      try {
        this.memory = (await this.file.load()) ?? {};
      } catch {
        this.memory = {};
      }
    }
    return this.memory;
  }
}
