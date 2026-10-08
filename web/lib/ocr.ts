/**
 * Reading photos and PDFs in the visitor's browser (static demo only).
 *
 * The static site has no server, so the page reads a photo or a scanned PDF
 * itself before handing it to the Python engine in the worker:
 *
 * - text: tesseract.js (Tesseract 5 LSTM in WebAssembly) with the Portuguese
 *   and English models;
 * - the fiscal QR code: jsQR on the photo or on the PDF's first pages;
 * - PDFs: pdf.js gives a born-digital PDF's own text and metadata (no OCR
 *   needed), and renders a scanned PDF's pages for OCR and the QR code;
 * - photo sharpness: the same edge measure the server uses, so a blurred
 *   photo becomes the same "take it again" task.
 *
 * Everything is self-hosted under /ocr/ (scripts/ocr-assets.mjs) and loaded
 * only when a photo or PDF is uploaded: the first load of the site is
 * unchanged, nothing is sent anywhere, no key is needed. What was read goes
 * with the upload as a device reading (lib/ocr-bridge.ts); the engine then
 * applies its normal Stage 0, field reading and verification. The upload,
 * reading included, is journaled like any change (lib/engine.ts), so a
 * reload replays it without reading the file again.
 */

import type * as PdfJs from "pdfjs-dist";
import { BASE_PATH } from "./mode";
import {
  EDGE,
  deviceReading,
  edgeSharpness,
  greyFromRgba,
  hasTextLayer,
  pageFromTesseract,
  textFromItems,
  type DeviceReading,
  type ReadPage,
  type TesseractBlock,
} from "./ocr-bridge";

export type { DeviceReading } from "./ocr-bridge";

/** Where scripts/ocr-assets.mjs puts the engines. */
const ASSETS = `${BASE_PATH}/ocr`;
const TESSERACT_VERSION = "7.0.0";
const LANGUAGES = ["por", "eng"];
/** Longer side of the image tesseract reads (larger photos are scaled down: text that small is unreadable). */
const MAX_SIDE = 2000;
/** PDF pages rendered at this scale (about 144 dpi) for OCR and QR codes. */
const PDF_SCALE = 2;
const MAX_PDF_PAGES = 10;
/** A born-digital PDF's fiscal QR code sits on its first page or two (as on the server). */
const QR_PDF_PAGES = 2;
/** The OCR worker is let go after this long without work, freeing its memory. */
const IDLE_MS = 120_000;

export type ReadingStage = "loading" | "reading";

interface TesseractWorker {
  recognize(
    image: HTMLCanvasElement,
    options?: Record<string, unknown>,
    output?: Record<string, boolean>,
  ): Promise<{ data: { blocks?: TesseractBlock[] | null } }>;
  terminate(): Promise<unknown>;
}

interface TesseractModule {
  createWorker(langs: string[], oem: number, options: Record<string, unknown>): Promise<TesseractWorker>;
}

function asset(path: string): string {
  return new URL(`${ASSETS}/${path}`, window.location.href).href;
}

/** What kind of file the browser can read: a photo, a PDF, or neither. */
export function readableKind(file: File): "image" | "pdf" | null {
  if (file.type.startsWith("image/")) return "image";
  if (file.type === "application/pdf" || /\.pdf$/i.test(file.name)) return "pdf";
  return null;
}

// ----------------------------------------------------------------- engines, loaded on first use

let tesseract: Promise<TesseractWorker> | null = null;
let idle: ReturnType<typeof setTimeout> | null = null;
let queue: Promise<unknown> = Promise.resolve();

function ocrWorker(): Promise<TesseractWorker> {
  tesseract ??= (async () => {
    const url = asset("tesseract/tesseract.esm.min.js");
    const mod = (await import(/* webpackIgnore: true */ /* turbopackIgnore: true */ url)) as { default: TesseractModule };
    return mod.default.createWorker(LANGUAGES, 1 /* LSTM only */, {
      workerPath: asset("tesseract/worker.min.js"),
      corePath: asset("tesseract/core"),
      langPath: asset("tesseract/lang"),
      gzip: true,
      cacheMethod: "none", // the browser's HTTP cache keeps the models; nothing else is stored
      workerBlobURL: false,
    });
  })();
  tesseract.catch(() => {
    tesseract = null; // a failed start is tried again on the next photo
  });
  return tesseract;
}

/** One OCR job at a time on the single worker; the worker is let go after a while without work. */
function recognize(canvas: HTMLCanvasElement): Promise<TesseractBlock[]> {
  const job = queue.then(async () => {
    if (idle) clearTimeout(idle);
    const worker = await ocrWorker();
    try {
      const { data } = await worker.recognize(canvas, {}, { blocks: true, text: false });
      return data.blocks ?? [];
    } finally {
      idle = setTimeout(() => {
        const current = tesseract;
        tesseract = null;
        void current?.then((w) => w.terminate()).catch(() => undefined);
      }, IDLE_MS);
    }
  });
  queue = job.catch(() => undefined);
  return job;
}

let jsqr: Promise<typeof import("jsqr").default> | null = null;

async function decodeQr(canvas: HTMLCanvasElement): Promise<string | null> {
  jsqr ??= import("jsqr").then((m) => m.default);
  const decode = await jsqr;
  // Full size first; a noisy photo's code is often found at a smaller size.
  const longest = Math.max(canvas.width, canvas.height);
  for (const side of longest > 1000 ? [longest, 1000] : [longest]) {
    const scaled = side === longest ? canvas : scaledCanvas(canvas, canvas.width, canvas.height, side);
    const ctx = scaled.getContext("2d", { willReadFrequently: true });
    if (!ctx) continue;
    const { data, width, height } = ctx.getImageData(0, 0, scaled.width, scaled.height);
    const found = decode(data, width, height, { inversionAttempts: "dontInvert" });
    if (found?.data) return found.data;
  }
  return null;
}

let pdfjs: Promise<typeof PdfJs> | null = null;

function loadPdfJs(): Promise<typeof PdfJs> {
  pdfjs ??= (async () => {
    const url = asset("pdfjs/pdf.min.mjs");
    const mod = (await import(/* webpackIgnore: true */ /* turbopackIgnore: true */ url)) as typeof PdfJs;
    mod.GlobalWorkerOptions.workerSrc = asset("pdfjs/pdf.worker.min.mjs");
    return mod;
  })();
  pdfjs.catch(() => {
    pdfjs = null;
  });
  return pdfjs;
}

// ----------------------------------------------------------------- pixels

function scaledCanvas(source: CanvasImageSource, width: number, height: number, maxSide: number): HTMLCanvasElement {
  const scale = Math.min(1, maxSide / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const ctx = canvas.getContext("2d", { willReadFrequently: true });
  if (!ctx) throw new Error("no 2D canvas");
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(source, 0, 0, canvas.width, canvas.height);
  return canvas;
}

function sharpness(source: CanvasImageSource, width: number, height: number): number | null {
  const small = scaledCanvas(source, width, height, EDGE.side);
  const ctx = small.getContext("2d", { willReadFrequently: true });
  if (!ctx) return null;
  const { data } = ctx.getImageData(0, 0, small.width, small.height);
  return edgeSharpness(greyFromRgba(data, small.width, small.height), small.width, small.height);
}

async function ocrPage(canvas: HTMLCanvasElement, number: number): Promise<ReadPage> {
  return pageFromTesseract(number, canvas.width, canvas.height, await recognize(canvas));
}

// ----------------------------------------------------------------- reading one file

async function readImage(file: File, onStage: (stage: ReadingStage) => void): Promise<DeviceReading> {
  const started = performance.now();
  const bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  try {
    const canvas = scaledCanvas(bitmap, bitmap.width, bitmap.height, MAX_SIDE);
    const measured = sharpness(bitmap, bitmap.width, bitmap.height);
    const qr = await decodeQr(canvas);
    if (!tesseract) onStage("loading");
    const ocr = ocrWorker();
    await ocr;
    onStage("reading");
    const page = await ocrPage(canvas, 1);
    return deviceReading({
      engine: "tesseract.js",
      version: `${TESSERACT_VERSION} ${LANGUAGES.join("+")}`,
      pages: [page],
      qr: [qr],
      sharpness: measured,
      ms: performance.now() - started,
    });
  } finally {
    bitmap.close();
  }
}

async function readPdf(file: File, onStage: (stage: ReadingStage) => void): Promise<DeviceReading> {
  const started = performance.now();
  onStage("loading");
  const lib = await loadPdfJs();
  const task = lib.getDocument({ data: new Uint8Array(await file.arrayBuffer()), wasmUrl: asset("pdfjs/wasm/") });
  const doc = await task.promise;
  try {
    const count = Math.min(doc.numPages, MAX_PDF_PAGES);
    const texts: string[] = [];
    for (let n = 1; n <= count; n += 1) {
      const page = await doc.getPage(n);
      texts.push(textFromItems((await page.getTextContent()).items as { str?: string; hasEOL?: boolean }[]));
    }
    const info = ((await Promise.resolve().then(() => doc.getMetadata()).catch(() => null))?.info ?? {}) as Record<string, unknown>;
    const scanned = !hasTextLayer(texts);
    const qr: (string | null)[] = [];
    const pages: ReadPage[] = [];
    for (let n = 1; n <= (scanned ? count : Math.min(count, QR_PDF_PAGES)); n += 1) {
      const page = await doc.getPage(n);
      const viewport = page.getViewport({ scale: PDF_SCALE });
      const canvas = document.createElement("canvas");
      canvas.width = Math.ceil(viewport.width);
      canvas.height = Math.ceil(viewport.height);
      await page.render({ canvas, viewport }).promise;
      qr.push(await decodeQr(canvas));
      if (scanned) {
        if (!tesseract) onStage("loading");
        await ocrWorker();
        onStage("reading");
        pages.push(await ocrPage(canvas, n));
      }
    }
    return deviceReading({
      engine: scanned ? "tesseract.js" : "pdf.js",
      version: scanned ? `${TESSERACT_VERSION} ${LANGUAGES.join("+")}` : lib.version,
      pages,
      qr,
      textLayer: texts,
      metadata: info,
      ms: performance.now() - started,
    });
  } finally {
    void task.destroy();
  }
}

/**
 * Read a photo or PDF in this browser. Null when it is neither, or when it
 * could not be read here (an image format the browser cannot open, the
 * engine failed to load): the file is then sent unread, and the engine says so.
 */
export async function readInBrowser(file: File, onStage: (stage: ReadingStage) => void = () => undefined): Promise<DeviceReading | null> {
  const kind = readableKind(file);
  try {
    if (kind === "image") return await readImage(file, onStage);
    if (kind === "pdf") return await readPdf(file, onStage);
  } catch (err) {
    console.warn("[ocr] could not read the file in this browser:", err);
  }
  return null;
}
