import { describe, expect, it } from "@jest/globals";
import { OfflinePipeline } from "../pipeline";
import { encodeEnvelope } from "../envelope";
import { utf8Encode } from "../../lib/bytes";
import type { CaptureInput, QueueSnapshot } from "../types";
import { bytesOf, harness, NodeCipher, sha256 } from "./fakes";

function input(text: string, extra: Partial<CaptureInput> = {}): CaptureInput {
  return {
    bytes: bytesOf(text),
    source: "mobile_scan",
    format: "image",
    fileName: "page-1.jpg",
    mimeType: "image/jpeg",
    capturedAt: "2026-09-28T10:00:00+01:00",
    ...extra,
  };
}

describe("capture", () => {
  it("encrypts locally and queues the item with the plaintext sha256", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    const res = await p.capture(input("Vodafone invoice FT 2026/183 total 92.40"));
    expect(res).toEqual({ status: "queued", id: "item-001" });

    const blob = h.blobs.blobs.get("item-001")!;
    expect(Buffer.from(blob).includes(Buffer.from("Vodafone"))).toBe(false);
    const item = h.queue.snapshot().items[0]!;
    expect(item.sha256).toBe(sha256(bytesOf("Vodafone invoice FT 2026/183 total 92.40")));
    expect(item.state).toBe("pending");
  });

  it("binds each blob to its item id, so a swapped blob cannot be opened", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("a"));
    const blob = h.blobs.blobs.get("item-001")!;
    await expect(h.cipher.open(blob, utf8Encode("item-002"))).rejects.toThrow();
  });

  it("rejects empty and oversized files without queueing", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps, { maxBytes: 10 });
    expect(await p.capture(input(""))).toEqual({ status: "rejected", reason: "empty" });
    expect(await p.capture(input("12345678901"))).toEqual({ status: "rejected", reason: "too_large" });
    expect(h.blobs.blobs.size).toBe(0);
  });

  it("does not queue the same bytes twice, nor bytes already delivered", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("same"));
    expect(await p.capture(input("same", { fileName: "copy.jpg" }))).toEqual({ status: "duplicate", id: "item-001" });
    await p.drain();
    expect(await p.capture(input("same"))).toEqual({ status: "already_sent", id: "item-001" });
    expect(h.server.requests).toHaveLength(1);
  });
});

describe("drain: deletion only after a verified receipt", () => {
  it("uploads sha256 + bytes, verifies the server hash, then deletes the local copy", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("receipt"));

    // At the moment of deletion, the verified receipt must already be on disk.
    const realDelete = h.blobs.delete.bind(h.blobs);
    let stateAtDelete: QueueSnapshot | null = null;
    h.blobs.delete = async (key: string) => {
      stateAtDelete = h.queue.snapshot();
      await realDelete(key);
    };

    const report = await p.drain();
    expect(report).toMatchObject({ attempted: 1, verified: 1, held: 0, offline: false });
    const req = h.server.requests[0]!;
    expect(req.sha256).toBe(sha256(bytesOf("receipt")));
    expect(Buffer.from(req.bytes).toString()).toBe("receipt");
    expect(req.idempotencyKey).toBe("item-001");

    expect(stateAtDelete!.items[0]).toMatchObject({ state: "verified", receipt: { sha256: req.sha256 } });
    expect(h.blobs.blobs.size).toBe(0);
    const snap = h.queue.snapshot();
    expect(snap.items).toHaveLength(0);
    expect(snap.sent[0]).toMatchObject({ id: "item-001", sha256: req.sha256, evidenceId: `ev_${req.sha256.slice(0, 8)}` });
  });

  it("keeps the file when the server hash does not match, and retries later", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("fragile"));
    h.server.script = ["corrupt"];

    const report = await p.drain();
    expect(report).toMatchObject({ attempted: 1, verified: 0, retrying: 1 });
    expect(h.blobs.blobs.has("item-001")).toBe(true);
    expect(h.blobs.log.some((o) => o.op === "delete")).toBe(false);
    const item = h.queue.snapshot().items[0]!;
    expect(item).toMatchObject({ state: "pending", lastFailure: "hash_mismatch", mismatches: 1 });
    expect(item.nextAttemptAt).toBeGreaterThan(h.clock.now());

    h.clock.advance(60_000);
    const second = await p.drain();
    expect(second.verified).toBe(1);
    expect(h.blobs.blobs.size).toBe(0);
  });

  it("treats an explicit hash_mismatch answer the same way", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("x"));
    h.server.script = [{ kind: "mismatch", status: 422 }];
    await p.drain();
    expect(h.blobs.blobs.has("item-001")).toBe(true);
    expect(h.queue.snapshot().items[0]).toMatchObject({ lastFailure: "hash_mismatch", lastStatus: 422 });
  });

  it("holds the item after repeated mismatches but never deletes it", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps, { maxMismatches: 3 });
    await p.capture(input("stubborn"));
    h.server.script = ["corrupt", "corrupt", "corrupt"];
    for (let i = 0; i < 3; i++) {
      await p.drain();
      h.clock.advance(3_600_000);
    }
    const item = h.queue.snapshot().items[0]!;
    expect(item).toMatchObject({ state: "held", lastFailure: "hash_mismatch", mismatches: 3 });
    expect(h.blobs.blobs.has("item-001")).toBe(true);

    // Held items are not retried automatically.
    const idle = await p.drain();
    expect(idle.attempted).toBe(0);

    // The owner can ask to try again.
    expect(await p.retryHeld("item-001")).toBe(true);
    const again = await p.drain();
    expect(again.verified).toBe(1);
    expect(h.blobs.blobs.size).toBe(0);
  });

  it("ignores a receipt that is not a valid sha256", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("y"));
    h.server.script = [{ kind: "receipt", sha256: "ok" }];
    await p.drain();
    expect(h.blobs.blobs.has("item-001")).toBe(true);
    expect(h.queue.snapshot().items[0]!.state).toBe("pending");
  });
});

describe("drain: retry and backoff", () => {
  it("backs off exponentially on server errors and waits until the item is due", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("retry me"));
    h.server.script = [
      { kind: "retryable", status: 503 },
      { kind: "retryable", status: 503 },
      { kind: "retryable", status: 500 },
    ];
    const delays: number[] = [];
    for (let i = 0; i < 3; i++) {
      const before = h.clock.now();
      await p.drain();
      const item = h.queue.snapshot().items[0]!;
      delays.push(item.nextAttemptAt - before);
      // Not due yet: nothing is attempted.
      expect((await p.drain()).attempted).toBe(0);
      h.clock.t = item.nextAttemptAt;
    }
    // base 5 s, factor 2, equal jitter with random()=0.5 -> 75% of nominal.
    expect(delays).toEqual([3_750, 7_500, 15_000]);
    const final = await p.drain();
    expect(final.verified).toBe(1);
  });

  it("honours Retry-After when it is longer than our own backoff", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("rate limited"));
    h.server.script = [{ kind: "retryable", status: 429, retryAfterMs: 120_000 }];
    const before = h.clock.now();
    await p.drain();
    expect(h.queue.snapshot().items[0]!.nextAttemptAt - before).toBe(120_000);
  });

  it("caps the delay at the policy maximum", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps, { backoff: { baseMs: 1_000, maxMs: 10_000, factor: 10, jitter: 0 } });
    await p.capture(input("cap"));
    h.server.script = Array.from({ length: 4 }, () => ({ kind: "retryable" as const, status: 502 }));
    let last = 0;
    for (let i = 0; i < 4; i++) {
      const before = h.clock.now();
      await p.drain();
      last = h.queue.snapshot().items[0]!.nextAttemptAt - before;
      h.clock.advance(last);
    }
    expect(last).toBe(10_000);
  });

  it("does nothing while offline and uploads once the network is back", async () => {
    const h = harness();
    h.network.online = false;
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("offline scan"));
    const report = await p.drain();
    expect(report.offline).toBe(true);
    expect(h.server.requests).toHaveLength(0);

    h.network.online = true;
    expect((await p.drain()).verified).toBe(1);
  });

  it("stops the drain when the connection drops mid-way, keeping every file", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("one"));
    await p.capture(input("two"));
    h.server.script = [{ kind: "network" }];
    const report = await p.drain();
    expect(report).toMatchObject({ attempted: 1, retrying: 1 });
    expect(h.server.requests).toHaveLength(1);
    expect(h.blobs.blobs.size).toBe(2);
  });

  it("treats a thrown transport error as a network failure", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("boom"));
    h.server.script = ["throw"];
    await p.drain();
    expect(h.queue.snapshot().items[0]).toMatchObject({ state: "pending", lastFailure: "network" });
    expect(h.blobs.blobs.size).toBe(1);
  });

  it("keeps retrying after sign-in problems without deleting anything", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("auth"));
    h.server.script = [{ kind: "auth", status: 401 }];
    await p.drain();
    expect(h.queue.snapshot().items[0]).toMatchObject({ state: "pending", lastFailure: "auth", lastStatus: 401 });
  });

  it("holds a rejected file for the owner instead of deleting it", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("unsupported"));
    h.server.script = [{ kind: "rejected", status: 415 }];
    const report = await p.drain();
    expect(report.held).toBe(1);
    expect(h.blobs.blobs.size).toBe(1);
    const summary = await p.summary();
    expect(summary.held).toEqual([{ id: "item-001", fileName: "page-1.jpg", format: "image", reason: "rejected" }]);
    expect(summary.waiting).toBe(0);
  });

  it("stops before starting new work once the deadline has passed", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("late"));
    const report = await p.drain({ deadline: h.clock.now() });
    expect(report).toMatchObject({ attempted: 0, stoppedEarly: true });
  });
});

describe("idempotent re-upload", () => {
  it("re-sends with the same idempotency key after a lost response and deletes once", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("lost response"));
    h.server.script = ["store-then-drop"];

    await p.drain();
    expect(h.server.stored.size).toBe(1);
    expect(h.blobs.blobs.size).toBe(1); // Stored remotely, but not yet confirmed: keep it.

    h.clock.advance(60_000);
    const report = await p.drain();
    expect(report.verified).toBe(1);
    expect(h.server.stored.size).toBe(1);
    expect(h.server.requests.map((r) => r.idempotencyKey)).toEqual(["item-001", "item-001"]);
    expect(h.blobs.log.filter((o) => o.op === "delete")).toHaveLength(1);
  });

  it("uploads each item once even when drains overlap", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("a"));
    await p.capture(input("b"));
    await Promise.all([p.drain(), p.drain(), p.drain()]);
    expect(h.server.requests).toHaveLength(2);
    expect(h.blobs.blobs.size).toBe(0);
  });
});

describe("recovery", () => {
  it("finishes a deletion interrupted after the receipt was verified", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("half done"));
    h.blobs.failDeletes = true;
    await p.drain();
    expect(h.queue.snapshot().items[0]!.state).toBe("verified");
    expect(h.blobs.blobs.size).toBe(1);

    h.blobs.failDeletes = false;
    const report = await new OfflinePipeline(h.deps).recover();
    expect(report.finalized).toBe(1);
    expect(h.blobs.blobs.size).toBe(0);
    expect(h.queue.snapshot().sent).toHaveLength(1);
    expect(h.server.requests).toHaveLength(1);
  });

  it("re-registers a blob written before a crash and uploads it", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    h.queue.failNextSave = true;
    await expect(p.capture(input("crash between writes", { fileName: "scan.jpg" }))).rejects.toThrow("disk full");
    expect(h.blobs.blobs.size).toBe(1);

    const report = await p.recover();
    expect(report.reRegistered).toBe(1);
    const item = h.queue.snapshot().items[0]!;
    expect(item.meta.fileName).toBe("scan.jpg");
    expect((await p.drain()).verified).toBe(1);
  });

  it("deletes an orphan whose bytes the server already confirmed", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("delivered"));
    await p.drain();
    // A stale copy of the same bytes left behind under another key.
    const meta = { source: "mobile_scan", format: "image", fileName: "x.jpg", mimeType: "image/jpeg", capturedAt: "2026-09-28T10:00:00Z" } as const;
    await h.blobs.write("stale", await h.cipher.seal(encodeEnvelope(meta, bytesOf("delivered")), utf8Encode("stale")));
    const report = await p.recover();
    expect(report.alreadySent).toBe(1);
    expect(h.blobs.blobs.size).toBe(0);
  });

  it("never deletes a blob it cannot decrypt", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    const other = new NodeCipher(new Uint8Array(32).fill(9));
    const meta = { source: "mobile_share", format: "pdf", fileName: "a.pdf", mimeType: "application/pdf", capturedAt: "2026-09-28T10:00:00Z" } as const;
    await h.blobs.write("foreign", await other.seal(encodeEnvelope(meta, bytesOf("%PDF")), utf8Encode("foreign")));
    const report = await p.recover();
    expect(report.unreadable).toBe(1);
    expect(h.blobs.blobs.has("foreign")).toBe(true);
  });

  it("releases an expired upload claim left by a crashed process", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("claimed"));
    const snap = h.queue.snapshot();
    snap.items[0]!.state = "uploading";
    snap.items[0]!.leaseUntil = h.clock.now() + 1_000;
    await h.queue.save(snap);

    expect((await p.drain()).attempted).toBe(0); // Claim still valid.
    h.clock.advance(1_000);
    expect((await p.recover()).released).toBe(1);
    expect((await p.drain()).verified).toBe(1);
  });

  it("holds an item whose local copy was tampered with, and keeps it", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("tamper"));
    const blob = h.blobs.blobs.get("item-001")!;
    blob[blob.length - 1] = blob[blob.length - 1]! ^ 0x01;
    const report = await p.drain();
    expect(report.held).toBe(1);
    expect(h.server.requests).toHaveLength(0);
    expect(h.blobs.blobs.has("item-001")).toBe(true);
    expect(h.queue.snapshot().items[0]).toMatchObject({ state: "held", lastFailure: "unreadable" });
  });

  it("marks queue entries whose local copy disappeared", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("gone"));
    h.blobs.blobs.clear();
    expect((await p.recover()).missing).toBe(1);
    expect(h.queue.snapshot().items[0]!.state).toBe("held");
  });
});

describe("owner actions and summary", () => {
  it("only discards items that are held", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    await p.capture(input("pending"));
    expect(await p.discardHeld("item-001")).toBe(false);
    expect(h.blobs.blobs.size).toBe(1);

    h.server.script = [{ kind: "rejected", status: 400 }];
    await p.drain();
    expect(await p.discardHeld("item-001")).toBe(true);
    expect(h.blobs.blobs.size).toBe(0);
    expect(h.queue.snapshot().items).toHaveLength(0);
  });

  it("notifies subscribers with plain counts", async () => {
    const h = harness();
    const p = new OfflinePipeline(h.deps);
    const seen: number[] = [];
    const stop = p.subscribe((s) => seen.push(s.waiting));
    await p.capture(input("1"));
    await p.capture(input("2"));
    await p.drain();
    stop();
    expect(seen[0]).toBe(1);
    expect(seen[1]).toBe(2);
    expect(seen[seen.length - 1]).toBe(0);
    const summary = await p.summary();
    expect(summary.sent).toHaveLength(2);
    expect(summary.nextDueAt).toBeNull();
  });
});
