/**
 * Money on the phone is a decimal string ("1492.30"), never a float.
 *
 * The backend serialises `Decimal` as a JSON string; older endpoints may still
 * send a JSON number. Both are normalised to a canonical decimal string and
 * formatted with digit arithmetic only, so nothing is lost to binary floats.
 */

/** Canonical decimal string: optional "-", digits, optional "." and digits. */
export type DecimalString = string & { readonly __decimal: unique symbol };

const DECIMAL_RE = /^-?\d+(\.\d+)?$/;

/**
 * Accept "92.40", "92.4", 92.4 or "-5". Rejects exponents, NaN, Infinity,
 * thousands separators and empty strings, returning null instead of guessing.
 */
export function toDecimal(value: unknown): DecimalString | null {
  let text: string;
  if (typeof value === "string") {
    text = value.trim();
  } else if (typeof value === "number" && Number.isFinite(value)) {
    // String(n) gives the shortest round-trip form; exponent forms are rejected below.
    text = String(value);
  } else {
    return null;
  }
  if (!DECIMAL_RE.test(text)) return null;
  return stripLeadingZeros(text) as DecimalString;
}

function stripLeadingZeros(text: string): string {
  const negative = text.startsWith("-");
  const body = negative ? text.slice(1) : text;
  const [intPart = "0", frac] = body.split(".");
  const int = intPart.replace(/^0+(?=\d)/, "");
  const isZero = /^0*$/.test(int) && (!frac || /^0*$/.test(frac));
  const sign = negative && !isZero ? "-" : "";
  return frac === undefined ? `${sign}${int}` : `${sign}${int}.${frac}`;
}

/** Round half away from zero to `places` decimals using digit arithmetic. */
export function roundDecimal(value: DecimalString, places: number): DecimalString {
  const negative = value.startsWith("-");
  const body = negative ? value.slice(1) : value;
  const [int = "0", frac = ""] = body.split(".");
  if (frac.length <= places) {
    const padded = places > 0 ? `${int}.${frac.padEnd(places, "0")}` : int;
    return stripLeadingZeros(`${negative ? "-" : ""}${padded}`) as DecimalString;
  }
  const kept = int + frac.slice(0, places);
  const roundUp = (frac.charCodeAt(places) - 48) >= 5;
  const digits = roundUp ? incrementDigits(kept) : kept;
  const intLen = digits.length - places;
  const out = places > 0 ? `${digits.slice(0, intLen)}.${digits.slice(intLen)}` : digits;
  return stripLeadingZeros(`${negative ? "-" : ""}${out}`) as DecimalString;
}

function incrementDigits(digits: string): string {
  const arr = digits.split("");
  for (let i = arr.length - 1; i >= 0; i--) {
    if (arr[i] === "9") {
      arr[i] = "0";
    } else {
      arr[i] = String.fromCharCode((arr[i] as string).charCodeAt(0) + 1);
      return arr.join("");
    }
  }
  return `1${arr.join("")}`;
}

function groupThousands(int: string): string {
  return int.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

const SYMBOLS: Readonly<Record<string, string>> = { EUR: "€", USD: "$", GBP: "£" };
/** ISO 4217 minor units for currencies that differ from 2. */
const MINOR_UNITS: Readonly<Record<string, number>> = { JPY: 0, KRW: 0, ISK: 0, CLP: 0, BHD: 3, KWD: 3, TND: 3 };

/**
 * "€1,492.30", "-€92.40", "1,200.00 CHF". Returns "" for an unreadable amount so
 * the UI never shows "NaN" (§70: no raw errors).
 */
export function formatMoney(amount: unknown, currency = "EUR"): string {
  const decimal = toDecimal(amount);
  if (decimal === null) return "";
  const code = currency.trim().toUpperCase();
  const places = MINOR_UNITS[code] ?? 2;
  const rounded = roundDecimal(decimal, places);
  const negative = rounded.startsWith("-");
  const [int = "0", frac] = (negative ? rounded.slice(1) : rounded).split(".");
  const number = frac ? `${groupThousands(int)}.${frac}` : groupThousands(int);
  const symbol = SYMBOLS[code];
  const body = symbol ? `${symbol}${number}` : `${number} ${code}`;
  return negative ? `-${body}` : body;
}
