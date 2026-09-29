import type { ISODate, MonthKey } from "./types";

const moneyFormatters = new Map<string, Intl.NumberFormat>();

/** €418.00 — always two decimals unless `whole` is set. */
export function formatMoney(amount: number, currency = "EUR", whole = false): string {
  const key = `${currency}:${whole}`;
  let f = moneyFormatters.get(key);
  if (!f) {
    f = new Intl.NumberFormat("en-IE", {
      style: "currency",
      currency,
      minimumFractionDigits: whole ? 0 : 2,
      maximumFractionDigits: whole ? 0 : 2,
    });
    moneyFormatters.set(key, f);
  }
  return f.format(amount);
}

const numberFormat = new Intl.NumberFormat("en-GB");

/** 12,482 */
export function formatNumber(n: number): string {
  return numberFormat.format(n);
}

function utcDate(iso: ISODate): Date {
  return new Date(`${iso.slice(0, 10)}T00:00:00Z`);
}

const dayMonth = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", timeZone: "UTC" });
/* Fixed three-letter months: ICU versions disagree on "Sep" vs "Sept". */
const SHORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const weekdayDayMonth = new Intl.DateTimeFormat("en-GB", {
  weekday: "long",
  day: "numeric",
  month: "long",
  timeZone: "UTC",
});
const monthName = new Intl.DateTimeFormat("en-GB", { month: "long", timeZone: "UTC" });
const monthYear = new Intl.DateTimeFormat("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
const timeFormat = new Intl.DateTimeFormat("en-GB", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "Europe/Lisbon",
});

/** 29 September */
export function formatDay(iso: ISODate): string {
  return dayMonth.format(utcDate(iso));
}

/** 29 Sep */
export function formatDayShort(iso: ISODate): string {
  const d = utcDate(iso);
  return `${d.getUTCDate()} ${SHORT_MONTHS[d.getUTCMonth()]}`;
}

/** September */
export function formatMonth(key: MonthKey): string {
  return monthName.format(utcDate(`${key}-01`));
}

/** Sep */
export function formatMonthShort(key: MonthKey): string {
  return SHORT_MONTHS[utcDate(`${key}-01`).getUTCMonth()] ?? key;
}

/** September 2026 */
export function formatMonthYear(key: MonthKey): string {
  return monthYear.format(utcDate(`${key}-01`));
}

/** 09:12 (Lisbon time, like the engine) */
export function formatTime(isoDateTime: string): string {
  return timeFormat.format(new Date(isoDateTime));
}

/** Calendar day (YYYY-MM-DD) of a timestamp, in Lisbon time. */
export function localDay(isoDateTime: string): ISODate {
  const parts = new Intl.DateTimeFormat("en-CA", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    timeZone: "Europe/Lisbon",
  }).format(new Date(isoDateTime));
  return parts;
}

/** "Today", "Yesterday" or "Tuesday 29 September". */
export function relativeDayLabel(day: ISODate, today: ISODate): string {
  const diff = Math.round((utcDate(today).getTime() - utcDate(day).getTime()) / 86_400_000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return weekdayDayMonth.format(utcDate(day));
}

/** "one", "two" … for small counts in sentences; digits above ten. */
export function countWord(n: number): string {
  const words = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"];
  return words[n] ?? String(n);
}

export function plural(n: number, one: string, many: string): string {
  return n === 1 ? one : many;
}
