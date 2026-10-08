/**
 * Portuguese tax number (NIF / NIPC) check for forms, the same rules and
 * words as the engine's validator (backend/src/backoffice/countries/pt/nif.py):
 * 9 digits, an assigned leading range, and a mod-11 check digit. "PT", spaces,
 * dots and dashes are formatting and are ignored. The server checks again.
 */

const PREFIXES = ["1", "2", "3", "45", "5", "6", "70", "71", "72", "74", "75", "77", "78", "79", "8", "90", "91", "98", "99"];
const WEIGHTS = [9, 8, 7, 6, 5, 4, 3, 2];
const SHAPE = /^(?:PT[ -]?)?([0-9 .-]+)$/i;
const FINAL_CONSUMER = "999999990";

export type NifCheck = { valid: true; normalized: string } | { valid: false; message: string };

/** "PT 123 456 789" → "123456789", or null when it is not 9 digits. */
export function normalizeNif(raw: string): string | null {
  const m = SHAPE.exec(raw.trim().split(/\s+/).join(" "));
  if (!m?.[1]) return null;
  const digits = m[1].replace(/[ .-]/g, "");
  return /^\d{9}$/.test(digits) ? digits : null;
}

export function nifCheckDigit(firstEight: string): number {
  const sum = WEIGHTS.reduce((acc, w, i) => acc + Number(firstEight[i]) * w, 0);
  const remainder = sum % 11;
  return remainder < 2 ? 0 : 11 - remainder;
}

export function checkNif(raw: string): NifCheck {
  const text = raw ?? "";
  if (!text.trim()) return { valid: false, message: "I need the NIF. It has 9 digits." };
  const nif = normalizeNif(text);
  if (nif === null) {
    const digits = text.replace(/[^0-9]/g, "");
    if (!digits || !SHAPE.test(text.trim().split(/\s+/).join(" "))) {
      return { valid: false, message: "A NIF only has digits. Please check it." };
    }
    return { valid: false, message: `That NIF has ${digits.length} digits. It needs 9.` };
  }
  if (!PREFIXES.some((p) => nif.startsWith(p))) {
    return { valid: false, message: "That NIF doesn’t look right. Please check the first digits." };
  }
  if (Number(nif[8]) !== nifCheckDigit(nif.slice(0, 8))) {
    return { valid: false, message: "That NIF doesn’t add up. Please check the digits." };
  }
  if (nif === FINAL_CONSUMER) {
    return { valid: false, message: "That’s the generic number used when there is no NIF. I need the real one." };
  }
  return { valid: true, normalized: nif };
}
