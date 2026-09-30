/**
 * Hands routed share items to the offline queue (§12 → §43) and picks the one
 * calm sentence the owner sees afterwards.
 */
import type { ShareIntent } from "expo-share-intent";
import { copy } from "../copy";
import { isoWithOffset } from "../lib/dates";
import type { CaptureSink } from "../scan/session";
import type { ShareRoute, SharePayload } from "./route";

export interface IngestDeps {
  sink: CaptureSink;
  readFile(path: string): Promise<Uint8Array>;
  /** Remove the app's own plaintext copy after sealing (never the sender's original). */
  discard(path: string): void;
  now(): Date;
}

export interface IngestResult {
  received: number;
  duplicates: number;
  unsupported: number;
  tooLarge: number;
  failed: number;
}

export function fromShareIntent(intent: ShareIntent): SharePayload {
  return {
    text: intent.text ?? null,
    webUrl: intent.webUrl,
    title: intent.meta?.title ?? null,
    files: (intent.files ?? []).map((f) => ({ path: f.path, mimeType: f.mimeType, fileName: f.fileName, size: f.size })),
  };
}

export async function ingestShare(route: ShareRoute, deps: IngestDeps): Promise<IngestResult> {
  const result: IngestResult = {
    received: 0,
    duplicates: 0,
    unsupported: route.skipped.filter((s) => s.reason === "unsupported").length,
    tooLarge: route.skipped.filter((s) => s.reason === "too_large").length,
    failed: 0,
  };
  const capturedAt = isoWithOffset(deps.now());
  for (const item of route.items) {
    try {
      const bytes = item.kind === "file" ? await deps.readFile(item.path) : item.bytes;
      const hints = item.kind === "inline" && item.title ? { hints: { title: item.title } } : {};
      const outcome = await deps.sink.capture({
        bytes,
        source: "mobile_share",
        format: item.format,
        fileName: item.fileName,
        mimeType: item.mimeType,
        capturedAt,
        ...(item.kind === "inline" && item.originalUrl ? { originalUrl: item.originalUrl } : {}),
        ...hints,
      });
      if (outcome.status === "queued") result.received += 1;
      else if (outcome.status === "duplicate" || outcome.status === "already_sent") result.duplicates += 1;
      else if (outcome.reason === "too_large") result.tooLarge += 1;
      else result.failed += 1;
      if (item.kind === "file" && outcome.status !== "rejected") deps.discard(item.path);
    } catch {
      result.failed += 1;
    }
  }
  return result;
}

/** One sentence. Success first; problems only when nothing useful arrived. */
export function shareMessage(result: IngestResult): string {
  const accepted = result.received + result.duplicates;
  if (accepted > 0) return result.received === 0 ? copy.scan.alreadyHave : copy.share.received(accepted);
  if (result.tooLarge > 0) return copy.share.tooLarge;
  if (result.unsupported > 0) return copy.share.unsupported;
  if (result.failed > 0) return copy.scan.failed;
  return copy.share.nothing;
}
