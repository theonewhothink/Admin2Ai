/**
 * Serialises async work inside one JS runtime, so the foreground drain, the
 * background task and new captures never edit the queue at the same time.
 * Cross-process safety comes from upload leases (see pipeline.ts).
 */
export class Mutex {
  private tail: Promise<void> = Promise.resolve();

  run<T>(task: () => Promise<T>): Promise<T> {
    const result = this.tail.then(task);
    this.tail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }
}
