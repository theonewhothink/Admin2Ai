/**
 * Durable storage on the device via expo-file-system.
 *
 * Sealed blobs live in the documents directory (never the cache, which the OS
 * may purge before an upload happens). Writes go to a temporary name first and
 * are then moved into place, so a crash never leaves a half-written file.
 */
import { Directory, File, Paths } from "expo-file-system";
import { isOwnedTemporary, isSafeKey, isScannerOutput } from "../platform";
import type { RawFile } from "../journal";
import type { BlobStore } from "../types";

const SEALED_EXT = ".sealed";
const TMP_EXT = ".tmp";

function ensureDir(dir: Directory): void {
  if (!dir.exists) dir.create({ intermediates: true, idempotent: true });
}

async function writeAtomic(dir: Directory, name: string, bytes: Uint8Array): Promise<void> {
  ensureDir(dir);
  const tmp = new File(dir, `${name}${TMP_EXT}`);
  if (tmp.exists) tmp.delete();
  tmp.write(bytes);
  await tmp.move(new File(dir, name), { overwrite: true });
}

export class ExpoBlobStore implements BlobStore {
  private readonly dir: Directory;

  constructor(folder = "evidence-queue") {
    this.dir = new Directory(Paths.document, folder);
  }

  async write(key: string, bytes: Uint8Array): Promise<void> {
    await writeAtomic(this.dir, this.name(key), bytes);
  }

  async read(key: string): Promise<Uint8Array | null> {
    const file = new File(this.dir, this.name(key));
    return file.exists ? file.bytes() : null;
  }

  async delete(key: string): Promise<void> {
    const file = new File(this.dir, this.name(key));
    if (file.exists) file.delete();
  }

  async list(): Promise<string[]> {
    if (!this.dir.exists) return [];
    return this.dir
      .list()
      .filter((entry): entry is File => entry instanceof File && entry.name.endsWith(SEALED_EXT))
      .map((file) => file.name.slice(0, -SEALED_EXT.length))
      .filter(isSafeKey);
  }

  private name(key: string): string {
    if (!isSafeKey(key)) throw new Error("unsafe blob key");
    return `${key}${SEALED_EXT}`;
  }
}

/** One small sealed file (queue index, cached screens) in the documents directory. */
export class ExpoRawFile implements RawFile {
  private readonly dir: Directory;

  constructor(
    private readonly fileName: string,
    folder = "backoffice-state",
  ) {
    this.dir = new Directory(Paths.document, folder);
  }

  async read(): Promise<Uint8Array | null> {
    const file = new File(this.dir, this.fileName);
    return file.exists ? file.bytes() : null;
  }

  writeAtomic(bytes: Uint8Array): Promise<void> {
    return writeAtomic(this.dir, this.fileName, bytes);
  }
}

/**
 * Roots whose files the app itself created: the cache, the iOS tmp folder
 * (VisionKit scanner output) and App Group containers (Share Extension copies).
 */
export function temporaryRoots(): string[] {
  const roots = [Paths.cache.uri, new Directory(Paths.document.parentDirectory, "tmp").uri];
  try {
    for (const dir of Object.values(Paths.appleSharedContainers)) roots.push(dir.uri);
  } catch {
    // Not available on Android.
  }
  return roots;
}

/** Read a captured or shared file into memory. */
export async function readFileBytes(uri: string): Promise<Uint8Array> {
  return new File(uri).bytes();
}

/** Size in bytes without reading the file, or null when unknown. */
export function fileSize(uri: string): number | null {
  try {
    const file = new File(uri);
    return file.exists ? file.size : null;
  } catch {
    return null;
  }
}

/**
 * Remove a plaintext copy once it is sealed in the queue. Only files inside the
 * app's own temporary roots, or the iOS scanner's page files, are deleted;
 * originals elsewhere are never touched.
 */
export function discardTemporary(uri: string): void {
  if (!isOwnedTemporary(uri, temporaryRoots()) && !isScannerOutput(uri, Paths.document.uri)) return;
  try {
    const file = new File(uri);
    if (file.exists) file.delete();
  } catch {
    // The OS clears temporary folders eventually; the sealed copy is what matters.
  }
}
