/**
 * What happens after the scanner returns (§11): check each page, ask about
 * retakes only when a page looks bad, then hand every page to the offline
 * queue (§43). No category, amount or supplier is ever asked for.
 */
import { copy } from "../copy";
import { isoWithOffset } from "../lib/dates";
import type { CaptureInput, CaptureResult, QualityIssue } from "../offline/types";
import type { PageAnalyzer, QrDetector, ScannedPage } from "./types";

export interface ReviewedPage {
  uri: string;
  issues: QualityIssue[];
  qr: string[];
}

export interface CaptureSink {
  capture(input: CaptureInput): Promise<CaptureResult>;
}

export interface SaveDeps {
  sink: CaptureSink;
  readFile(uri: string): Promise<Uint8Array>;
  /** Remove the scanner's plaintext page once it is sealed in the queue. */
  discard(uri: string): void;
  newCaptureId(): string;
  now(): Date;
}

export interface SaveSummary {
  queued: number;
  duplicates: number;
  tooLarge: number;
  failed: number;
}

/** Analyse pages one by one (keeps memory low). Failures mean "no opinion", never a block. */
export async function inspectPages(
  pages: readonly ScannedPage[],
  analyzer: PageAnalyzer,
  qr: QrDetector,
): Promise<ReviewedPage[]> {
  const out: ReviewedPage[] = [];
  for (const page of pages) {
    const [issues, codes] = await Promise.all([
      analyzer.analyze(page.uri).catch((): QualityIssue[] => []),
      qr.detect(page.uri).catch((): string[] => []),
    ]);
    out.push({ uri: page.uri, issues, qr: codes });
  }
  return out;
}

export function needsReview(pages: readonly ReviewedPage[]): boolean {
  return pages.some((p) => p.issues.length > 0);
}

/** One plain sentence per problem, e.g. "Page 2 looks blurry." */
export function reviewNotes(pages: readonly ReviewedPage[]): Array<{ page: number; text: string }> {
  const notes: Array<{ page: number; text: string }> = [];
  pages.forEach((p, i) => {
    for (const issue of p.issues) notes.push({ page: i + 1, text: copy.scan.issue(issue, i + 1, pages.length) });
  });
  return notes;
}

/** Swap in a retaken page, keeping the order. The old page's temp file is discarded. */
export function replacePage(
  pages: readonly ReviewedPage[],
  index: number,
  replacement: ReviewedPage,
  discard: (uri: string) => void,
): ReviewedPage[] {
  if (index < 0 || index >= pages.length) return pages.slice();
  const old = pages[index];
  if (old && old.uri !== replacement.uri) discard(old.uri);
  return pages.map((p, i) => (i === index ? replacement : p));
}

function mimeFor(uri: string): { mimeType: string; ext: string } {
  const lower = uri.toLowerCase();
  if (lower.endsWith(".png")) return { mimeType: "image/png", ext: "png" };
  if (lower.endsWith(".heic")) return { mimeType: "image/heic", ext: "heic" };
  return { mimeType: "image/jpeg", ext: "jpg" };
}

/**
 * Seal every page into the queue as one capture. A page's plaintext file is
 * removed only after the queue holds an encrypted copy (or already had it).
 */
export async function savePages(pages: readonly ReviewedPage[], deps: SaveDeps): Promise<SaveSummary> {
  const summary: SaveSummary = { queued: 0, duplicates: 0, tooLarge: 0, failed: 0 };
  const captureId = deps.newCaptureId();
  const capturedAt = isoWithOffset(deps.now());
  for (const [i, page] of pages.entries()) {
    const { mimeType, ext } = mimeFor(page.uri);
    const input: Omit<CaptureInput, "bytes"> = {
      source: "mobile_scan",
      format: "image",
      fileName: `scan-${capturedAt.slice(0, 10)}-p${i + 1}.${ext}`,
      mimeType,
      capturedAt,
      captureId,
      page: i + 1,
      pageCount: pages.length,
    };
    const hints = {
      ...(page.qr.length ? { qr: page.qr } : {}),
      ...(page.issues.length ? { quality: page.issues } : {}),
    };
    let result: CaptureResult;
    try {
      const bytes = await deps.readFile(page.uri);
      result = await deps.sink.capture({ ...input, ...(Object.keys(hints).length ? { hints } : {}), bytes });
    } catch {
      summary.failed += 1;
      continue;
    }
    if (result.status === "queued") summary.queued += 1;
    else if (result.status === "duplicate" || result.status === "already_sent") summary.duplicates += 1;
    else if (result.reason === "too_large") summary.tooLarge += 1;
    else summary.failed += 1;
    if (result.status !== "rejected") deps.discard(page.uri);
  }
  return summary;
}

/** The single calm sentence shown after saving. */
export function saveMessage(summary: SaveSummary, online: boolean): string {
  if (summary.failed > 0) return copy.scan.failed;
  if (summary.tooLarge > 0) return copy.scan.tooLarge;
  if (summary.queued === 0 && summary.duplicates > 0) return copy.scan.alreadyHave;
  return online ? copy.scan.saved : copy.scan.savedOffline;
}
