import { describe, expect, it } from "@jest/globals";
import { OfflinePipeline } from "../pipeline";
import { QueueRunner, type Timers } from "../runner";
import { bytesOf, harness } from "./fakes";

class ManualTimers implements Timers {
  scheduled: Array<{ fn: () => void; ms: number; id: number }> = [];
  private next = 1;
  setTimeout(fn: () => void, ms: number): unknown {
    const id = this.next++;
    this.scheduled.push({ fn, ms, id });
    return id;
  }
  clearTimeout(handle: unknown): void {
    this.scheduled = this.scheduled.filter((t) => t.id !== handle);
  }
  fireAll(): void {
    const due = this.scheduled;
    this.scheduled = [];
    for (const t of due) t.fn();
  }
}

const settle = () => new Promise((r) => setTimeout(r, 0));

async function setup(online = true) {
  const h = harness();
  h.network.online = online;
  const pipeline = new OfflinePipeline(h.deps);
  await pipeline.capture({
    bytes: bytesOf("scan"),
    source: "mobile_scan",
    format: "image",
    fileName: "p1.jpg",
    mimeType: "image/jpeg",
    capturedAt: "2026-09-28T10:00:00+01:00",
  });
  const timers = new ManualTimers();
  const runner = new QueueRunner(pipeline, h.network, h.clock, timers);
  return { h, pipeline, timers, runner };
}

describe("QueueRunner", () => {
  it("drains as soon as it starts", async () => {
    const { h, runner } = await setup();
    const stop = runner.start();
    await settle();
    await runner.kick();
    expect(h.server.requests).toHaveLength(1);
    expect(h.blobs.blobs.size).toBe(0);
    stop();
  });

  it("waits while offline and drains when the network returns", async () => {
    const { h, runner, timers } = await setup(false);
    const stop = runner.start();
    await runner.kick();
    expect(h.server.requests).toHaveLength(0);
    expect(timers.scheduled).toHaveLength(0);

    h.network.set(true);
    await settle();
    await runner.kick();
    expect(h.server.requests.length).toBeGreaterThanOrEqual(1);
    expect(h.blobs.blobs.size).toBe(0);
    stop();
  });

  it("schedules the next retry for when the item falls due", async () => {
    const { h, runner, timers } = await setup();
    h.server.script = [{ kind: "retryable", status: 503 }];
    const stop = runner.start();
    await settle();
    await runner.kick();
    expect(timers.scheduled).toHaveLength(1);
    expect(timers.scheduled[0]!.ms).toBe(3_750);

    h.clock.advance(3_750);
    timers.fireAll();
    await settle();
    await runner.kick();
    expect(h.blobs.blobs.size).toBe(0);
    stop();
  });

  it("stops listening and clears its timer when stopped", async () => {
    const { h, runner, timers } = await setup();
    h.server.script = [{ kind: "retryable", status: 503 }];
    const stop = runner.start();
    await settle();
    await runner.kick();
    stop();
    expect(timers.scheduled).toHaveLength(0);
    const before = h.server.requests.length;
    h.network.set(true);
    await settle();
    expect(h.server.requests.length).toBe(before);
  });

  it("reports errors instead of throwing", async () => {
    const { h, pipeline, timers } = await setup();
    const errors: unknown[] = [];
    h.queue.failNextSave = true;
    const runner = new QueueRunner(pipeline, h.network, h.clock, timers, (e) => errors.push(e));
    expect(await runner.kick()).toBeNull();
    expect(errors).toHaveLength(1);
  });
});
