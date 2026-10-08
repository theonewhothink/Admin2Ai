/**
 * The pure half of reading in the browser (no DOM, no network): what the
 * OCR engine, the QR decoder and pdf.js found, turned into the device
 * reading the Python engine takes with an upload (`POST /api/evidence` with
 * `reading`; backend/src/backoffice/reading/browser.py has the receiving
 * side and the same limits). lib/ocr.ts does the reading itself.
 *
 * Kept free of imports and of non-erasable TypeScript so `node --test`
 * runs it directly (tests/ocr.test.mjs).
 */

/** The wire format's method: an OCR reading made on the visitor's device. */
export const WIRE_METHOD = "ocr_browser";
/** Lines tesseract is less sure of than this (percent) are noise, typically the QR code read as letters. */
export const MIN_LINE_CONFIDENCE = 40;
/** A PDF with fewer non-blank characters than this has no text layer (backend extraction/pdf.py). */
export const MIN_TEXT_CHARS = 20;
/** Most pages, lines, QR codes the engine accepts (reading/browser.py). */
export const LIMITS = { pages: 20, lines: 600, lineChars: 400, qr: 8, qrChars: 2000 } as const;
/** Edge sharpness, measured exactly as the server does (extraction/quality.py `edge_sharpness`). */
export const EDGE = { side: 600, tile: 16, contrast: 80, minTiles: 4 } as const;

export type Box = [number, number, number, number];

export interface ReadLine {
  text: string;
  /** 0-1 */
  confidence: number;
  /** Pixels of the page image as read: x0, y0, x1, y1. */
  box: Box;
}

export interface ReadPage {
  number: number;
  width: number;
  height: number;
  lines: ReadLine[];
}

export interface DeviceReading {
  method: typeof WIRE_METHOD;
  engine: string;
  version: string;
  pages: ReadPage[];
  /** QR codes decoded on the photo or on the PDF's first pages. */
  qr: string[];
  /** A PDF's own text, one entry per page (pdf.js), so a born-digital PDF needs no OCR. */
  textLayer?: string[];
  /** A PDF's metadata (Producer, Creator, Title, Subject, Keywords). */
  metadata?: Record<string, string>;
  /** Edge sharpness of a photo, 0-1 (null: not measured). */
  sharpness: number | null;
  ms: number;
}

/** One line as tesseract.js returns it (`recognize(..., { blocks: true })`). */
export interface TesseractLine {
  text: string;
  /** 0-100 */
  confidence: number;
  bbox: { x0: number; y0: number; x1: number; y1: number };
}

export interface TesseractBlock {
  paragraphs: { lines: TesseractLine[] }[];
}

const HAS_WORD = /[\p{L}\p{N}]/u;

/** The lines of one page: the sure ones, with text, a 0-1 score and a box, in the order read. */
export function pageFromTesseract(number: number, width: number, height: number, blocks: TesseractBlock[] | null | undefined): ReadPage {
  const lines: ReadLine[] = [];
  for (const block of blocks ?? []) {
    for (const paragraph of block.paragraphs ?? []) {
      for (const line of paragraph.lines ?? []) {
        const text = (line.text ?? "").replace(/\s+/g, " ").trim().slice(0, LIMITS.lineChars);
        const { x0, y0, x1, y1 } = line.bbox ?? { x0: 0, y0: 0, x1: 0, y1: 0 };
        if (!text || !HAS_WORD.test(text) || !(line.confidence >= MIN_LINE_CONFIDENCE) || !(x1 > x0 && y1 > y0)) continue;
        lines.push({ text, confidence: Math.round(line.confidence * 10) / 1000, box: [x0, y0, x1, y1] });
        if (lines.length >= LIMITS.lines) return { number, width, height, lines };
      }
    }
  }
  return { number, width, height, lines };
}

/** A pdf.js page's text items joined into lines. */
export function textFromItems(items: ReadonlyArray<{ str?: string; hasEOL?: boolean }>): string {
  let out = "";
  for (const item of items) {
    if (typeof item.str === "string") out += item.str;
    if (item.hasEOL) out += "\n";
  }
  return out;
}

/** True when the pages carry their own text (a born-digital PDF), as the server decides it. */
export function hasTextLayer(pages: readonly string[]): boolean {
  return pages.reduce((n, text) => n + text.replace(/\s+/g, "").length, 0) >= MIN_TEXT_CHARS;
}

/** QR code strings found, without blanks or repeats, within the engine's limits. */
export function qrCodes(found: ReadonlyArray<string | null | undefined>): string[] {
  const out: string[] = [];
  for (const text of found) {
    const value = (text ?? "").trim();
    if (value && value.length <= LIMITS.qrChars && !out.includes(value)) out.push(value);
  }
  return out.slice(0, LIMITS.qr);
}

/** Greyscale (ITU-R 601 luma, as Pillow's "L") of RGBA pixels. */
export function greyFromRgba(rgba: ArrayLike<number>, width: number, height: number): Float32Array {
  const out = new Float32Array(width * height);
  for (let i = 0, p = 0; i < out.length; i += 1, p += 4) {
    out[i] = (rgba[p]! * 299 + rgba[p + 1]! * 587 + rgba[p + 2]! * 114) / 1000;
  }
  return out;
}

function percentile(sorted: ArrayLike<number>, q: number): number {
  // numpy's default (linear interpolation between closest ranks)
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos);
  const hi = Math.ceil(pos);
  return sorted[lo]! + (sorted[hi]! - sorted[lo]!) * (pos - lo);
}

/**
 * How sharp the print in a greyscale image is, 0-1 (higher is sharper), or
 * null when there is no print. The image should already be scaled to
 * EDGE.side on its longer side. Same algorithm as the server: per 16-pixel
 * tile with print (1st-99th percentile range at least 80), the steepest step
 * between neighbouring pixels over that range; the score is the median of
 * the sharpest quarter of tiles.
 */
export function edgeSharpness(grey: ArrayLike<number>, width: number, height: number): number | null {
  const t = EDGE.tile;
  const th = Math.floor(height / t);
  const tw = Math.floor(width / t);
  if (th === 0 || tw === 0) return null;
  const ratios: number[] = [];
  const values = new Float32Array(t * t);
  for (let ty = 0; ty < th; ty += 1) {
    for (let tx = 0; tx < tw; tx += 1) {
      let steep = 0;
      let k = 0;
      for (let y = ty * t; y < (ty + 1) * t; y += 1) {
        for (let x = tx * t; x < (tx + 1) * t; x += 1) {
          const v = grey[y * width + x]!;
          values[k++] = v;
          if (x + 1 < width) steep = Math.max(steep, Math.abs(grey[y * width + x + 1]! - v));
          if (y + 1 < height) steep = Math.max(steep, Math.abs(grey[(y + 1) * width + x]! - v));
        }
      }
      values.sort();
      const contrast = percentile(values, 0.99) - percentile(values, 0.01);
      if (contrast >= EDGE.contrast) ratios.push(Math.min(steep / contrast, 1));
    }
  }
  if (ratios.length < EDGE.minTiles) return null;
  ratios.sort((a, b) => b - a);
  const best = ratios.slice(0, Math.max(1, Math.floor(ratios.length / 4))).sort((a, b) => a - b);
  const mid = best.length / 2;
  const median = best.length % 2 ? best[Math.floor(mid)]! : (best[mid - 1]! + best[mid]!) / 2;
  return Math.round(median * 10000) / 10000;
}

/** What the browser read from one file, in the engine's wire format. */
export function deviceReading(parts: {
  engine: string;
  version: string;
  pages: ReadPage[];
  qr: ReadonlyArray<string | null | undefined>;
  textLayer?: string[];
  metadata?: Record<string, unknown>;
  sharpness?: number | null;
  ms: number;
}): DeviceReading {
  const reading: DeviceReading = {
    method: WIRE_METHOD,
    engine: parts.engine,
    version: parts.version,
    pages: parts.pages.slice(0, LIMITS.pages).map((p, i) => ({ ...p, number: i + 1 })),
    qr: qrCodes(parts.qr),
    sharpness: typeof parts.sharpness === "number" && Number.isFinite(parts.sharpness) ? parts.sharpness : null,
    ms: Math.max(0, Math.round(parts.ms)),
  };
  if (parts.textLayer) reading.textLayer = parts.textLayer.slice(0, LIMITS.pages);
  if (parts.metadata) {
    const metadata: Record<string, string> = {};
    for (const key of ["Producer", "Creator", "Title", "Subject", "Keywords"]) {
      const value = parts.metadata[key];
      if (typeof value === "string" && value.trim()) metadata[key] = value.slice(0, 2000);
    }
    reading.metadata = metadata;
  }
  return reading;
}
