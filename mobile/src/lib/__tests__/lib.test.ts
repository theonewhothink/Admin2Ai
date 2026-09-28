import { describe, expect, it } from "@jest/globals";
import {
  bytesEqual,
  concatBytes,
  fromBase64,
  normalizeHexDigest,
  readUint32,
  toBase64,
  toHex,
  utf8Decode,
  utf8Encode,
  writeUint32,
} from "../bytes";
import { formatMoney, roundDecimal, toDecimal, type DecimalString } from "../money";
import { formatDay, formatMonth, formatSince, formatTime, greetingFor, relativeDayLabel } from "../dates";

describe("bytes", () => {
  it("round-trips UTF-8 like TextEncoder/TextDecoder, including astral characters", () => {
    for (const s of ["", "€92.40", "Fatura nº 2026/183 — São João", "emoji 🧾 receipt", "日本語"]) {
      expect(Array.from(utf8Encode(s))).toEqual(Array.from(new TextEncoder().encode(s)));
      expect(utf8Decode(utf8Encode(s))).toBe(s);
    }
  });

  it("replaces malformed sequences instead of throwing", () => {
    expect(utf8Decode(Uint8Array.from([0x61, 0xc3, 0x28, 0x62]))).toBe("a�(b");
    expect(utf8Decode(Uint8Array.from([0xed, 0xa0, 0x80]))).toContain("�"); // encoded surrogate
    expect(utf8Encode("\ud800")).toEqual(utf8Encode("�"));
  });

  it("base64 matches Node and rejects garbage", () => {
    for (const len of [0, 1, 2, 3, 4, 5, 31, 32, 33]) {
      const b = Uint8Array.from({ length: len }, (_, i) => (i * 37 + 11) & 0xff);
      expect(toBase64(b)).toBe(Buffer.from(b).toString("base64"));
      expect(bytesEqual(fromBase64(toBase64(b)), b)).toBe(true);
    }
    expect(() => fromBase64("a$==")).toThrow();
    expect(() => fromBase64("abcde")).toThrow();
  });

  it("hex digests are lower-case and validated", () => {
    expect(toHex(Uint8Array.from([0, 15, 255]))).toBe("000fff");
    expect(normalizeHexDigest(" " + "AB".repeat(32) + "\n")).toBe("ab".repeat(32));
    expect(normalizeHexDigest("ab")).toBeNull();
    expect(normalizeHexDigest("zz".repeat(32))).toBeNull();
  });

  it("uint32 and concat helpers", () => {
    expect(readUint32(writeUint32(0xdeadbeef), 0)).toBe(0xdeadbeef);
    expect(() => writeUint32(-1)).toThrow(RangeError);
    expect(() => readUint32(Uint8Array.of(1, 2), 0)).toThrow(RangeError);
    expect(Array.from(concatBytes([Uint8Array.of(1), Uint8Array.of(), Uint8Array.of(2, 3)]))).toEqual([1, 2, 3]);
  });
});

describe("money (decimal strings, never floats)", () => {
  it("normalises strings and numbers, rejecting anything ambiguous", () => {
    expect(toDecimal("92.40")).toBe("92.40");
    expect(toDecimal(92.4)).toBe("92.4");
    expect(toDecimal(" 0005.10 ")).toBe("5.10");
    expect(toDecimal("-0.00")).toBe("0.00");
    for (const bad of ["1,492.30", "1e3", "", "abc", NaN, Infinity, null, undefined, 1e21, "12."]) {
      expect(toDecimal(bad)).toBeNull();
    }
  });

  it("rounds half away from zero with digit arithmetic", () => {
    const d = (s: string) => s as DecimalString;
    expect(roundDecimal(d("1.005"), 2)).toBe("1.01"); // 1.005 as a float would round down
    expect(roundDecimal(d("9.995"), 2)).toBe("10.00");
    expect(roundDecimal(d("-2.345"), 2)).toBe("-2.35");
    expect(roundDecimal(d("-0.004"), 2)).toBe("0.00");
    expect(roundDecimal(d("7"), 2)).toBe("7.00");
    expect(roundDecimal(d("7.5"), 0)).toBe("8");
  });

  it("formats like the spec examples", () => {
    expect(formatMoney("1492.30", "EUR")).toBe("€1,492.30");
    expect(formatMoney(92.4, "EUR")).toBe("€92.40");
    expect(formatMoney("-117.2", "eur")).toBe("-€117.20");
    expect(formatMoney("1200", "CHF")).toBe("1,200.00 CHF");
    expect(formatMoney("1234567.891", "JPY")).toBe("1,234,568 JPY");
    expect(formatMoney("0.1", "GBP")).toBe("£0.10");
    expect(formatMoney("not money", "EUR")).toBe("");
  });
});

describe("dates (plain language)", () => {
  it("formats days and months without ICU", () => {
    expect(formatDay("2026-09-29")).toBe("29 September");
    expect(formatDay("2026-02-30")).toBe("");
    expect(formatMonth("2026-09")).toBe("September");
  });

  it("labels relative days", () => {
    expect(relativeDayLabel("2026-10-02", "2026-10-02")).toBe("Today");
    expect(relativeDayLabel("2026-10-01", "2026-10-02")).toBe("Yesterday");
    expect(relativeDayLabel("2026-09-29", "2026-10-02")).toBe("Tuesday 29 September");
  });

  it("describes when a connection last synced (§47-48)", () => {
    // Tests run in Europe/Lisbon (UTC+1 in summer).
    const now = new Date("2026-10-02T09:00:00+01:00");
    expect(formatSince("2026-10-01T14:42:00+01:00", now)).toBe("14:42 yesterday");
    expect(formatSince("2026-10-02T08:05:00+01:00", now)).toBe("08:05 today");
    expect(formatSince("2026-09-28T17:30:00+01:00", now)).toBe("28 September at 17:30");
    expect(formatSince("garbage", now)).toBe("");
    expect(formatTime("2026-10-02T09:12:00+02:00")).toBe("08:12");
  });

  it("greets by local hour", () => {
    expect(greetingFor(new Date("2026-10-02T08:00:00+01:00"))).toBe("Good morning.");
    expect(greetingFor(new Date("2026-10-02T13:00:00+01:00"))).toBe("Good afternoon.");
    expect(greetingFor(new Date("2026-10-02T21:00:00+01:00"))).toBe("Good evening.");
  });
});
