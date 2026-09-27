import type { Evidence } from "./types";

/**
 * Evidence ids are namespaced: "month:<company>:<yyyy-mm>", "needs:<id>",
 * "company:<id>". Documents ("doc:"), transactions ("txn:") and emails have no
 * page yet and render as plain chips.
 */
export function evidenceHref(e: Evidence): string | null {
  const [kind, a, b] = e.id.split(":");
  if (kind === "month" && a && b) return `/companies/${a}?month=${b}`;
  if (kind === "needs" && a) return `/needs-you#${a}`;
  if (kind === "company" && a) return `/companies/${a}`;
  return null;
}

export type EvidenceKind = "document" | "payment" | "month" | "decision" | "other";

export function evidenceKind(e: Evidence): EvidenceKind {
  const kind = e.id.split(":")[0];
  if (kind === "doc" || kind === "email") return "document";
  if (kind === "txn") return "payment";
  if (kind === "month" || kind === "company") return "month";
  if (kind === "needs") return "decision";
  return "other";
}
