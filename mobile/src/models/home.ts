/**
 * Home (§41, §69): one status sentence, three tiles, then the companies.
 *
 * - "Everything is under control." only when nothing needs the owner and every
 *   connection is syncing. A stale connection means the month can never show
 *   green (§47-48), so it becomes "Action required." with a reconnect banner.
 * - Emerald is used only for "all good / closed" (§30-33): the month tile turns
 *   emerald at 100% with healthy connections, never for 94%.
 */
import { copy } from "../copy";
import { formatSince, greetingFor } from "../lib/dates";
import type { Connection, HomeData, Tone } from "../api/types";

export interface Tile {
  id: "needs" | "month" | "handled";
  label: string;
  value: string;
  tone: Tone;
}

export interface HomeView {
  greeting: string;
  status: { text: string; tone: Tone };
  tiles: Tile[];
  banner: { text: string; tone: Tone } | null;
  companies: HomeData["companies"];
}

/** "Gmail needs reconnecting. Your email has not synced since 14:42 yesterday." (§47-48) */
export function reconnectMessage(connection: Connection, now: Date): string {
  const since = connection.lastSyncedLabel ?? formatSince(connection.lastSyncedAt, now);
  const what = connection.kind === "email" ? "Your email has" : connection.kind === "bank" ? "Your bank has" : "It has";
  return since
    ? `${connection.name} needs reconnecting. ${what} not synced since ${since}.`
    : `${connection.name} needs reconnecting.`;
}

/** Handled today: the explicit mobile field, else the sum of today's stats, else null. */
export function handledToday(home: HomeData): number | null {
  if (home.handledToday !== undefined) return home.handledToday;
  if ((home.handledPeriodLabel ?? "").trim().toLowerCase() === "today") {
    return home.handled.reduce((n, h) => n + h.count, 0);
  }
  return null;
}

export function buildHomeView(home: HomeData, needsYouCount: number, now: Date): HomeView {
  const stale = home.connections.filter((c) => c.status === "stale");
  const needs = Math.max(0, needsYouCount);
  const status =
    stale.length > 0
      ? { text: copy.status.actionRequired, tone: "attention" as const }
      : needs > 0
        ? { text: copy.status.needs(needs), tone: "attention" as const }
        : { text: copy.status.allGood, tone: "good" as const };

  const month = home.currentMonth;
  const closed = month.percentClosed >= 100 && stale.length === 0;
  const tiles: Tile[] = [
    { id: "needs", label: copy.tiles.needsYou, value: String(needs), tone: needs > 0 ? "attention" : "neutral" },
    { id: "month", label: month.label, value: `${Math.floor(month.percentClosed)}%`, tone: closed ? "good" : "neutral" },
  ];
  const handled = handledToday(home);
  if (handled !== null) tiles.push({ id: "handled", label: copy.tiles.handledToday, value: String(handled), tone: "neutral" });

  const first = stale[0];
  return {
    greeting: home.greeting ?? greetingFor(now),
    status,
    tiles,
    banner: first ? { text: reconnectMessage(first, now), tone: "attention" } : null,
    companies: home.companies,
  };
}
