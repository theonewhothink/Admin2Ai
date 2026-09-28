import { describe, expect, it } from "@jest/globals";
import { utf8Decode } from "../../lib/bytes";
import type { CaptureInput, CaptureResult } from "../../offline/types";
import { fromShareIntent, ingestShare, shareMessage, type IngestDeps } from "../ingest";
import { redirectSharePath } from "../nativeIntent";
import { formatFor, httpUrl, resolveMime, routeShare, safeFileName } from "../route";

const MAX = 1_000;

describe("routeShare", () => {
  it("accepts PDFs, images, screenshots, .eml and XML e-invoices", () => {
    const route = routeShare(
      {
        files: [
          { path: "file:///c/a.pdf", mimeType: "application/pdf", fileName: "Fatura FT 2026-183.pdf", size: 10 },
          { path: "file:///c/b.jpg", mimeType: "image/jpeg", fileName: "IMG_0042.jpg", size: 10 },
          { path: "file:///c/c.png", mimeType: "image/png", fileName: "Screenshot 2026-09-28 at 10.14.png", size: 10 },
          { path: "file:///c/d.eml", mimeType: "application/octet-stream", fileName: "invoice.eml", size: 10 },
          { path: "file:///c/e.xml", mimeType: "text/xml", fileName: "ubl.xml", size: 10 },
        ],
      },
      MAX,
    );
    expect(route.skipped).toEqual([]);
    expect(route.items.map((i) => i.format)).toEqual(["pdf", "image", "screenshot", "eml", "xml"]);
    expect(route.items[3]).toMatchObject({ mimeType: "message/rfc822" });
  });

  it("skips videos and oversized files with a reason", () => {
    const route = routeShare(
      {
        files: [
          { path: "file:///c/v.mov", mimeType: "video/quicktime", fileName: "v.mov", size: 10 },
          { path: "file:///c/big.pdf", mimeType: "application/pdf", fileName: "big.pdf", size: MAX + 1 },
          { path: null, mimeType: "application/pdf", fileName: "ghost.pdf", size: 1 },
        ],
      },
      MAX,
    );
    expect(route.items).toEqual([]);
    expect(route.skipped).toEqual([
      { name: "v.mov", reason: "unsupported" },
      { name: "big.pdf", reason: "too_large" },
    ]);
  });

  it("turns a bare link into a URL item the server will follow (§9)", () => {
    const route = routeShare({ text: "https://portal.vodafone.pt/invoice/183", webUrl: "https://portal.vodafone.pt/invoice/183", title: "Invoice" }, MAX);
    expect(route.items).toHaveLength(1);
    const item = route.items[0]!;
    expect(item).toMatchObject({ kind: "inline", format: "url", mimeType: "text/uri-list", originalUrl: "https://portal.vodafone.pt/invoice/183", title: "Invoice" });
    if (item.kind === "inline") expect(utf8Decode(item.bytes)).toBe("https://portal.vodafone.pt/invoice/183\r\n");
  });

  it("keeps the words of a text share and flags its link", () => {
    const text = "Your invoice is ready. View it: https://billing.example.pt/i/77).";
    const route = routeShare({ text, webUrl: null }, MAX);
    expect(route.items[0]).toMatchObject({ kind: "inline", format: "text", originalUrl: "https://billing.example.pt/i/77" });
  });

  it("refuses dangerous or malformed links", () => {
    expect(httpUrl("javascript:alert(1)")).toBeNull();
    expect(httpUrl("file:///etc/passwd")).toBeNull();
    expect(httpUrl("https://")).toBeNull();
    expect(routeShare({ webUrl: "intent://x", text: "" }, MAX).items).toEqual([]);
  });

  it("names and types files safely", () => {
    expect(safeFileName("../../etc/passwd", "x")).toBe("passwd");
    expect(safeFileName("a\u0000b.pdf", "x")).toBe("ab.pdf");
    expect(safeFileName("..", "fallback")).toBe("fallback");
    expect(resolveMime("scan.HEIC", null)).toBe("image/heic");
    expect(resolveMime("x.pdf", "application/pdf; name=x.pdf")).toBe("application/pdf");
    expect(formatFor("a.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")).toBeNull();
    expect(formatFor("Captura de ecrã 2026.png", "image/png")).toBe("screenshot");
  });

  it("maps expo-share-intent payloads", () => {
    const payload = fromShareIntent({
      text: null,
      webUrl: null,
      type: "file",
      meta: { title: "t" },
      files: [{ path: "file:///x.pdf", mimeType: "application/pdf", fileName: "x.pdf", size: 3, width: null, height: null, duration: null }],
    });
    expect(payload).toEqual({
      text: null,
      webUrl: null,
      title: "t",
      files: [{ path: "file:///x.pdf", mimeType: "application/pdf", fileName: "x.pdf", size: 3 }],
    });
  });

  it("keeps share deep links away from the router", () => {
    expect(redirectSharePath("backoffice://dataUrl=backofficeShareKey?nonce=1#file")).toBe("/");
    expect(redirectSharePath("/needs-you")).toBe("/needs-you");
  });
});

describe("ingestShare", () => {
  function deps(results: CaptureResult[], opts: { failRead?: boolean } = {}) {
    const captured: CaptureInput[] = [];
    const discarded: string[] = [];
    const d: IngestDeps = {
      sink: {
        async capture(input) {
          captured.push(input);
          return results.shift() ?? { status: "queued", id: `q${captured.length}` };
        },
      },
      async readFile(path) {
        if (opts.failRead) throw new Error("gone");
        return new TextEncoder().encode(`bytes of ${path}`);
      },
      discard: (p) => discarded.push(p),
      now: () => new Date("2026-09-28T10:14:03+01:00"),
    };
    return { d, captured, discarded };
  }

  it("queues each item as mobile_share and removes the app's plaintext copies", async () => {
    const route = routeShare(
      { files: [{ path: "file:///cache/a.pdf", mimeType: "application/pdf", fileName: "a.pdf", size: 3 }], text: "https://x.pt/i", webUrl: "https://x.pt/i" },
      MAX,
    );
    const { d, captured, discarded } = deps([]);
    const result = await ingestShare(route, d);
    expect(result).toMatchObject({ received: 2, failed: 0 });
    expect(captured.map((c) => [c.source, c.format])).toEqual([["mobile_share", "pdf"], ["mobile_share", "url"]]);
    expect(captured[0]!.capturedAt).toBe("2026-09-28T10:14:03+01:00");
    expect(captured[1]!.originalUrl).toBe("https://x.pt/i");
    expect(discarded).toEqual(["file:///cache/a.pdf"]);
    expect(shareMessage(result)).toBe("Got two items. I'll take it from here.");
  });

  it("keeps the file when it could not be queued", async () => {
    const route = routeShare({ files: [{ path: "file:///cache/a.pdf", mimeType: "application/pdf", fileName: "a.pdf", size: 3 }] }, MAX);
    const { d, discarded } = deps([{ status: "rejected", reason: "too_large" }]);
    const result = await ingestShare(route, d);
    expect(result.tooLarge).toBe(1);
    expect(discarded).toEqual([]);
    expect(shareMessage(result)).toBe("This file is too large to send from the phone.");
  });

  it("explains plainly when nothing was usable", async () => {
    const unsupported = routeShare({ files: [{ path: "file:///v.mov", mimeType: "video/mp4", fileName: "v.mov", size: 1 }] }, MAX);
    expect(shareMessage(await ingestShare(unsupported, deps([]).d))).toBe(
      "I can't use this kind of file. Share a photo, PDF, email or link instead.",
    );
    const failing = routeShare({ files: [{ path: "file:///a.pdf", mimeType: "application/pdf", fileName: "a.pdf", size: 1 }] }, MAX);
    expect(shareMessage(await ingestShare(failing, deps([], { failRead: true }).d))).toBe("I couldn't save that scan. Please try again.");
    expect(shareMessage(await ingestShare({ items: [], skipped: [] }, deps([]).d))).toBe("There was nothing I could use in that share.");
    const dup = routeShare({ text: "https://a.pt", webUrl: "https://a.pt" }, MAX);
    expect(shareMessage(await ingestShare(dup, deps([{ status: "duplicate", id: "q1" }]).d))).toBe("I already have this one.");
  });
});
