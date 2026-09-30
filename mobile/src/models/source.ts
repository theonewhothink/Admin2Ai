/** A quiet line telling the owner when the screen is not showing live data. */
import { copy } from "../copy";
import { formatSince } from "../lib/dates";
import type { Loaded } from "../api/client";

export function sourceNote(loaded: Pick<Loaded<unknown>, "source" | "asOf" | "reason">, now: Date): string | null {
  if (loaded.source === "live") return null;
  if (loaded.source === "cached" && loaded.asOf !== null) {
    return `${copy.offlineNote} ${formatSince(new Date(loaded.asOf).toISOString(), now)}.`;
  }
  return loaded.reason === "unreachable" ? copy.unreachableSample : copy.sampleNote;
}
