/**
 * Share → Back Office (§12). Turns what another app shared (WhatsApp, Gmail,
 * Outlook, Safari, Chrome, Photos, Files) into items for the offline queue.
 *
 * Accepts URLs, PDFs, images, screenshots, email exports (.eml), text and XML
 * e-invoices. Everything else is skipped with a plain reason. Nothing is
 * classified here: the server decides what the evidence is (§13).
 */
import { utf8Encode } from "../lib/bytes";
import type { CaptureFormat } from "../offline/types";

/** Normalised share payload (mapped from expo-share-intent's ShareIntent). */
export interface SharePayload {
  text?: string | null;
  webUrl?: string | null;
  title?: string | null;
  files?: ReadonlyArray<{ path: string | null; mimeType: string | null; fileName: string | null; size: number | null }> | null;
}

export type ShareItem =
  | { kind: "file"; path: string; fileName: string; mimeType: string; format: CaptureFormat; size: number | null }
  | {
      kind: "inline";
      bytes: Uint8Array;
      fileName: string;
      mimeType: string;
      format: "url" | "text";
      originalUrl?: string;
      title?: string;
    };

export interface ShareSkip {
  name: string;
  reason: "unsupported" | "too_large";
}

export interface ShareRoute {
  items: ShareItem[];
  skipped: ShareSkip[];
}

const EXT_MIME: Readonly<Record<string, string>> = {
  pdf: "application/pdf",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  png: "image/png",
  heic: "image/heic",
  heif: "image/heif",
  webp: "image/webp",
  gif: "image/gif",
  tif: "image/tiff",
  tiff: "image/tiff",
  eml: "message/rfc822",
  txt: "text/plain",
  xml: "application/xml",
};

/** Screenshot file names used by iOS and Android in the languages we serve first. */
const SCREENSHOT_RE = /(screenshot|screen shot|captura de (pantalla|ecr[aã])|capture d[’']?[eé]cran|bildschirmfoto|schermata)/i;

function extensionOf(name: string): string {
  const match = /\.([a-z0-9]{1,5})$/i.exec(name);
  return match ? (match[1] as string).toLowerCase() : "";
}

function baseMime(mime: string | null): string {
  return (mime ?? "").split(";")[0]!.trim().toLowerCase();
}

/** Resolve the MIME type, preferring the file name when the sender says only "octet-stream". */
export function resolveMime(fileName: string, declared: string | null): string {
  const mime = baseMime(declared);
  const byExt = EXT_MIME[extensionOf(fileName)];
  if (!mime || mime === "application/octet-stream" || mime === "*/*") return byExt ?? "application/octet-stream";
  return mime;
}

/** Which evidence format a shared file is, or null when we cannot use it. */
export function formatFor(fileName: string, mime: string): CaptureFormat | null {
  if (mime === "application/pdf") return "pdf";
  if (mime.startsWith("image/")) return SCREENSHOT_RE.test(fileName) ? "screenshot" : "image";
  if (mime === "message/rfc822" || extensionOf(fileName) === "eml") return "eml";
  if (mime === "application/xml" || mime === "text/xml" || extensionOf(fileName) === "xml") return "xml";
  if (mime === "text/plain") return "text";
  return null;
}

/** Keep only a safe display name: no folders, no control characters. */
export function safeFileName(name: string | null, fallback: string): string {
  const base = (name ?? "").split(/[\\/]/).pop() ?? "";
  const clean = base.replace(/[\u0000-\u001f\u007f]/g, "").trim();
  return clean.length > 0 && clean !== "." && clean !== ".." ? clean.slice(0, 180) : fallback;
}

/** An http(s) URL with a host, or null. Other schemes (javascript:, file:, intent:) are refused. */
export function httpUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (!/^https?:\/\/[^\s/?#]+[^\s]*$/i.test(trimmed)) return null;
  return trimmed;
}

function firstHttpUrl(text: string): string | null {
  const match = /https?:\/\/[^\s<>"']+/i.exec(text);
  return match ? httpUrl(match[0].replace(/[).,;:!?]+$/, "")) : null;
}

export function routeShare(payload: SharePayload, maxBytes: number): ShareRoute {
  const items: ShareItem[] = [];
  const skipped: ShareSkip[] = [];

  for (const [i, file] of (payload.files ?? []).entries()) {
    if (!file.path) continue;
    const fileName = safeFileName(file.fileName ?? file.path, `shared-${i + 1}`);
    const mimeType = resolveMime(fileName, file.mimeType);
    const format = formatFor(fileName, mimeType);
    if (!format) {
      skipped.push({ name: fileName, reason: "unsupported" });
    } else if (file.size !== null && file.size > maxBytes) {
      skipped.push({ name: fileName, reason: "too_large" });
    } else {
      items.push({ kind: "file", path: file.path, fileName, mimeType, format, size: file.size });
    }
  }

  const text = (payload.text ?? "").trim();
  const url = httpUrl(payload.webUrl) ?? (text ? firstHttpUrl(text) : null);
  const title = payload.title?.trim() || undefined;
  if (url && (text === "" || text === url)) {
    // Just a link: the server follows it (§9 link intelligence).
    items.push({
      kind: "inline",
      bytes: utf8Encode(`${url}\r\n`),
      fileName: "shared-link.uri",
      mimeType: "text/uri-list",
      format: "url",
      originalUrl: url,
      ...(title ? { title } : {}),
    });
  } else if (text) {
    // Text with context ("Your invoice is ready: https://…"): keep the words, flag the link.
    const bytes = utf8Encode(text);
    if (bytes.length > maxBytes) {
      skipped.push({ name: "shared-text.txt", reason: "too_large" });
    } else {
      items.push({
        kind: "inline",
        bytes,
        fileName: "shared-text.txt",
        mimeType: "text/plain; charset=utf-8",
        format: "text",
        ...(url ? { originalUrl: url } : {}),
        ...(title ? { title } : {}),
      });
    }
  }
  return { items, skipped };
}
