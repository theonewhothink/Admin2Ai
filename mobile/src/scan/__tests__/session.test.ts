import { describe, expect, it } from "@jest/globals";
import type { CaptureInput, CaptureResult } from "../../offline/types";
import { inspectPages, needsReview, replacePage, reviewNotes, saveMessage, savePages, type SaveDeps } from "../session";
import type { PageAnalyzer, QrDetector } from "../types";

const analyzer: PageAnalyzer = {
  async analyze(uri) {
    if (uri.includes("blurry")) return ["blurry"];
    if (uri.includes("broken")) throw new Error("decode failed");
    return [];
  },
};
const qr: QrDetector = {
  async detect(uri) {
    return uri.includes("qr") ? ["A:500000000*B:999999990*C:PT*D:FT*G:FT 2026/183*O:483.60"] : [];
  },
};

function saveDeps(results: CaptureResult[] = []) {
  const captured: CaptureInput[] = [];
  const discarded: string[] = [];
  const deps: SaveDeps = {
    sink: {
      async capture(input) {
        captured.push(input);
        return results.shift() ?? { status: "queued", id: String(captured.length) };
      },
    },
    readFile: async (uri) => new TextEncoder().encode(uri),
    discard: (uri) => discarded.push(uri),
    newCaptureId: () => "cap-1",
    now: () => new Date("2026-09-28T10:14:03+01:00"),
  };
  return { deps, captured, discarded };
}

describe("scan review", () => {
  it("collects issues and QR codes per page; analyzer failures mean no opinion", async () => {
    const pages = await inspectPages(
      [{ uri: "file:///tmp/p1-qr.jpg" }, { uri: "file:///tmp/p2-blurry.jpg" }, { uri: "file:///tmp/p3-broken.jpg" }],
      analyzer,
      qr,
    );
    expect(pages.map((p) => p.issues)).toEqual([[], ["blurry"], []]);
    expect(pages[0]!.qr).toHaveLength(1);
    expect(needsReview(pages)).toBe(true);
    expect(reviewNotes(pages)).toEqual([{ page: 2, text: "Page 2 looks blurry." }]);
  });

  it("words single-page notes without page numbers", () => {
    expect(reviewNotes([{ uri: "a", issues: ["glare", "too_dark"], qr: [] }]).map((n) => n.text)).toEqual([
      "There's glare on the page.",
      "The page is too dark.",
    ]);
  });

  it("replaces a retaken page in place and drops the old file", () => {
    const discarded: string[] = [];
    const pages = [
      { uri: "a", issues: [], qr: [] },
      { uri: "b", issues: ["blurry" as const], qr: [] },
    ];
    const next = replacePage(pages, 1, { uri: "c", issues: [], qr: [] }, (u) => discarded.push(u));
    expect(next.map((p) => p.uri)).toEqual(["a", "c"]);
    expect(discarded).toEqual(["b"]);
    expect(replacePage(pages, 5, { uri: "z", issues: [], qr: [] }, () => undefined)).toEqual(pages);
  });
});

describe("savePages", () => {
  it("queues every page as one capture with page numbers and hints", async () => {
    const { deps, captured, discarded } = saveDeps();
    const summary = await savePages(
      [
        { uri: "file:///tmp/1.jpg", issues: [], qr: ["QR"] },
        { uri: "file:///tmp/2.png", issues: ["glare"], qr: [] },
      ],
      deps,
    );
    expect(summary).toEqual({ queued: 2, duplicates: 0, tooLarge: 0, failed: 0 });
    expect(captured.map((c) => [c.captureId, c.page, c.pageCount, c.mimeType, c.source, c.format])).toEqual([
      ["cap-1", 1, 2, "image/jpeg", "mobile_scan", "image"],
      ["cap-1", 2, 2, "image/png", "mobile_scan", "image"],
    ]);
    expect(captured[0]!.hints).toEqual({ qr: ["QR"] });
    expect(captured[1]!.hints).toEqual({ quality: ["glare"] });
    expect(captured[0]!.fileName).toBe("scan-2026-09-28-p1.jpg");
    expect(discarded).toEqual(["file:///tmp/1.jpg", "file:///tmp/2.png"]);
    expect(saveMessage(summary, true)).toBe("Got it. I'll take it from here.");
    expect(saveMessage(summary, false)).toBe("Saved on this phone. I'll send it when you're online.");
  });

  it("keeps the plaintext page when the queue refused it", async () => {
    const { deps, discarded } = saveDeps([{ status: "rejected", reason: "too_large" }]);
    const summary = await savePages([{ uri: "file:///tmp/1.jpg", issues: [], qr: [] }], deps);
    expect(summary.tooLarge).toBe(1);
    expect(discarded).toEqual([]);
    expect(saveMessage(summary, true)).toBe("This file is too large to send from the phone.");
  });

  it("reports duplicates and failures plainly", async () => {
    const dup = saveDeps([{ status: "already_sent", id: "x" }]);
    expect(saveMessage(await savePages([{ uri: "a", issues: [], qr: [] }], dup.deps), true)).toBe("I already have this one.");

    const broken = saveDeps();
    broken.deps.readFile = async () => {
      throw new Error("io");
    };
    const summary = await savePages([{ uri: "a", issues: [], qr: [] }], broken.deps);
    expect(summary.failed).toBe(1);
    expect(broken.discarded).toEqual([]);
    expect(saveMessage(summary, true)).toBe("I couldn't save that scan. Please try again.");
  });
});
