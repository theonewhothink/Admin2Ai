"use client";

import { useAnswered } from "@/lib/resolved-store";
import type { CompanySummary } from "@/lib/types";
import { Status } from "./ui";

/** Company status that turns "On track" once its pending answers are given. */
export function CompanyStatus({ company }: { company: Pick<CompanySummary, "tone" | "statusLabel" | "pendingItemIds"> }) {
  const answered = useAnswered();
  const pending = company.pendingItemIds ?? [];
  const cleared = pending.length > 0 && pending.every((id) => answered.has(id));
  return cleared ? <Status tone="good" label="On track" /> : <Status tone={company.tone} label={company.statusLabel} />;
}
