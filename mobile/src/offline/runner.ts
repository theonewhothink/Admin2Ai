/**
 * Foreground scheduling for the offline queue (§43): drain when the network
 * comes back, when the app returns to the foreground, after a capture, and when
 * the next retry falls due. The background task (./expo/backgroundTask.ts)
 * covers the time the app is closed.
 */
import type { DrainReport, OfflinePipeline } from "./pipeline";
import type { Clock, NetworkMonitor } from "./types";

export interface Timers {
  setTimeout(fn: () => void, ms: number): unknown;
  clearTimeout(handle: unknown): void;
}

export const systemTimers: Timers = {
  setTimeout: (fn, ms) => setTimeout(fn, ms),
  clearTimeout: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
};

/** Never poll faster than this, even if an item is overdue. */
const MIN_RESCHEDULE_MS = 1_000;

export class QueueRunner {
  private timer: unknown = null;
  private inFlight: Promise<DrainReport | null> | null = null;
  private rerun = false;
  private stopped = true;

  constructor(
    private readonly pipeline: OfflinePipeline,
    private readonly network: NetworkMonitor,
    private readonly clock: Clock,
    private readonly timers: Timers = systemTimers,
    private readonly onError: (error: unknown) => void = () => undefined,
  ) {}

  /** Start listening. Returns a stop function. */
  start(): () => void {
    this.stopped = false;
    const unsubscribe = this.network.subscribe((online) => {
      if (online) void this.kick();
    });
    void this.kick();
    return () => {
      this.stopped = true;
      unsubscribe();
      this.clearTimer();
    };
  }

  /** Drain now. Overlapping calls are coalesced into one follow-up run. */
  kick(): Promise<DrainReport | null> {
    if (this.inFlight) {
      this.rerun = true;
      return this.inFlight;
    }
    this.inFlight = this.run().finally(() => {
      this.inFlight = null;
      if (this.rerun && !this.stopped) {
        this.rerun = false;
        void this.kick();
      }
    });
    return this.inFlight;
  }

  private async run(): Promise<DrainReport | null> {
    this.clearTimer();
    try {
      const report = await this.pipeline.drain();
      if (!report.offline) await this.scheduleNext();
      return report;
    } catch (error) {
      this.onError(error);
      return null;
    }
  }

  private async scheduleNext(): Promise<void> {
    if (this.stopped) return;
    const { nextDueAt } = await this.pipeline.summary();
    if (nextDueAt === null) return;
    const delay = Math.max(MIN_RESCHEDULE_MS, nextDueAt - this.clock.now());
    this.timer = this.timers.setTimeout(() => {
      this.timer = null;
      void this.kick();
    }, delay);
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      this.timers.clearTimeout(this.timer);
      this.timer = null;
    }
  }
}
