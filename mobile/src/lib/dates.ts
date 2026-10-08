/**
 * Plain-language dates (§36). Fixed English names so output does not depend on
 * the ICU build shipped with Hermes. Times are shown in the phone's own zone.
 */

export type ISODate = string; // "2026-10-02"
export type ISODateTime = string; // "2026-10-02T09:12:00+02:00"

const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
] as const;
const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"] as const;

const ISO_DATE_RE = /^(\d{4})-(\d{2})-(\d{2})$/;

/** Parse "YYYY-MM-DD" (or the date part of a timestamp) as a calendar day. */
function parseDay(iso: ISODate): { y: number; m: number; d: number } | null {
  const match = ISO_DATE_RE.exec(iso.slice(0, 10));
  if (!match) return null;
  const y = Number(match[1]);
  const m = Number(match[2]);
  const d = Number(match[3]);
  const check = new Date(Date.UTC(y, m - 1, d));
  if (check.getUTCFullYear() !== y || check.getUTCMonth() !== m - 1 || check.getUTCDate() !== d) return null;
  return { y, m, d };
}

/** "29 September". Empty string for an unreadable date. */
export function formatDay(iso: ISODate): string {
  const p = parseDay(iso);
  return p ? `${p.d} ${MONTHS[p.m - 1]}` : "";
}

/** "September" from "2026-09". */
export function formatMonth(key: string): string {
  const p = parseDay(`${key.slice(0, 7)}-01`);
  return p ? (MONTHS[p.m - 1] as string) : "";
}

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/** Calendar day of an instant in the phone's time zone. */
export function localDayKey(instant: Date): ISODate {
  return `${instant.getFullYear()}-${pad2(instant.getMonth() + 1)}-${pad2(instant.getDate())}`;
}

/** "09:12" in the phone's time zone. Empty string for an unreadable timestamp. */
export function formatTime(value: ISODateTime | Date): string {
  const d = value instanceof Date ? value : new Date(value);
  return Number.isNaN(d.getTime()) ? "" : `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

function dayDiff(day: ISODate, today: ISODate): number | null {
  const a = parseDay(day);
  const b = parseDay(today);
  if (!a || !b) return null;
  return Math.round((Date.UTC(b.y, b.m - 1, b.d) - Date.UTC(a.y, a.m - 1, a.d)) / 86_400_000);
}

/** "Today", "Yesterday" or "Tuesday 29 September". */
export function relativeDayLabel(day: ISODate, today: ISODate): string {
  const diff = dayDiff(day, today);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  const p = parseDay(day);
  if (!p) return "";
  const weekday = WEEKDAYS[new Date(Date.UTC(p.y, p.m - 1, p.d)).getUTCDay()];
  return `${weekday} ${p.d} ${MONTHS[p.m - 1]}`;
}

/** "14:42 today", "14:42 yesterday" or "29 September at 14:42" (§47-48 reconnect copy). */
export function formatSince(value: ISODateTime, now: Date): string {
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return "";
  const diff = dayDiff(localDayKey(d), localDayKey(now));
  const time = formatTime(d);
  if (diff === 0) return `${time} today`;
  if (diff === 1) return `${time} yesterday`;
  return `${formatDay(localDayKey(d))} at ${time}`;
}

/** "Good morning." / "Good afternoon." / "Good evening." for the phone's local hour. */
export function greetingFor(now: Date): string {
  const h = now.getHours();
  if (h >= 5 && h < 12) return "Good morning.";
  if (h >= 12 && h < 18) return "Good afternoon.";
  return "Good evening.";
}

/** "2026-09-28T10:14:03+01:00": local wall time with the phone's UTC offset. */
export function isoWithOffset(instant: Date): ISODateTime {
  const offset = -instant.getTimezoneOffset();
  const sign = offset >= 0 ? "+" : "-";
  const abs = Math.abs(offset);
  return (
    `${localDayKey(instant)}T${pad2(instant.getHours())}:${pad2(instant.getMinutes())}:${pad2(instant.getSeconds())}` +
    `${sign}${pad2(Math.floor(abs / 60))}:${pad2(abs % 60)}`
  );
}
