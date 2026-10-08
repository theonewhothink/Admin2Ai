/** Activity: a quiet timeline of what was handled, grouped by day (§42 quiet success). */
import { localDayKey, relativeDayLabel } from "../lib/dates";
import type { ActivityItem, Tone } from "../api/types";

export interface ActivityGroup {
  day: string;
  label: string;
  items: ActivityItem[];
}

/** Newest first, grouped by the phone's local day. Unreadable timestamps are dropped. */
export function groupActivity(items: readonly ActivityItem[], today: string): ActivityGroup[] {
  const dated = items
    .map((item) => ({ item, t: new Date(item.at).getTime() }))
    .filter((x) => !Number.isNaN(x.t))
    .sort((a, b) => b.t - a.t);
  const groups: ActivityGroup[] = [];
  for (const { item, t } of dated) {
    const day = localDayKey(new Date(t));
    let group = groups[groups.length - 1];
    if (!group || group.day !== day) {
      group = { day, label: relativeDayLabel(day, today), items: [] };
      groups.push(group);
    }
    group.items.push(item);
  }
  return groups;
}

/** Protective actions read as attention; closures as good; the rest stay neutral. */
export function activityTone(kind: ActivityItem["kind"]): Tone {
  if (kind === "closed") return "good";
  if (kind === "protected") return "attention";
  return "neutral";
}
