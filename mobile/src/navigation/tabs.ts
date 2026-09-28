/** Mobile navigation (§40): Home, Needs You, Scan (large, centre), Activity, Ask. */
import type { IconName } from "../ui/Icon";

export interface TabSpec {
  /** expo-router route name inside app/(tabs). */
  route: "index" | "needs-you" | "scan" | "activity" | "ask";
  label: string;
  icon: IconName;
  center?: boolean;
}

export const TABS: readonly TabSpec[] = [
  { route: "index", label: "Home", icon: "home" },
  { route: "needs-you", label: "Needs You", icon: "needs" },
  { route: "scan", label: "Scan", icon: "scan", center: true },
  { route: "activity", label: "Activity", icon: "activity" },
  { route: "ask", label: "Ask", icon: "ask" },
];

export function tabFor(route: string): TabSpec | undefined {
  return TABS.find((t) => t.route === route);
}

/** Badge text for the Needs You tab: hidden at 0, capped at "9+". */
export function badgeText(count: number): string | null {
  if (!Number.isFinite(count) || count <= 0) return null;
  return count > 9 ? "9+" : String(Math.floor(count));
}
