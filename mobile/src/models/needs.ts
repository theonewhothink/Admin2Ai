/**
 * Needs You decisions (§34-41): decisions, not forms. Choice items answer in
 * one tap with an optional "remember" rule. Approval items (changed bank
 * details, §26) need a phone check, a tick and a fresh identity check, and are
 * never remembered.
 */
import { copy } from "../copy";
import type { NeedsYouApprovalItem, NeedsYouChoiceItem, NeedsYouItem, RememberRule } from "../api/types";

/** Checkbox label before a choice is made: "Always use this answer for IKEA paid with card •••• 4817". */
export function rememberLabel(rule: RememberRule | undefined): string {
  if (!rule) return copy.needs.rememberFallback;
  return rule.template.includes("{choice}") ? rule.template.replace("{choice}", "this answer") : rule.template;
}

/** The rule in plain words once the choice is known (override wins). */
export function rememberSentence(rule: RememberRule, choiceId: string, choiceLabel: string): string {
  return rule.overrides?.[choiceId] ?? rule.template.replace("{choice}", choiceLabel);
}

/** Message after a one-tap answer (§5: "I will remember this."). */
export function choiceDoneMessage(item: NeedsYouChoiceItem, remember: boolean): string {
  return remember && item.remember ? copy.doneRemember : copy.done;
}

export type ApprovalAction =
  | { kind: "keep_blocked"; optionId: string; message: string }
  | { kind: "release"; optionId: string; message: string; needsIdentityCheck: true };

/**
 * What an approval card may send. Release requires the owner's explicit tick
 * that the supplier confirmed by phone; without it there is nothing to send.
 */
export function approvalAction(item: NeedsYouApprovalItem, choice: "keep" | "release", phoneConfirmed: boolean): ApprovalAction | null {
  if (choice === "keep") return { kind: "keep_blocked", optionId: item.keepBlocked.optionId, message: item.keepBlocked.message };
  if (!phoneConfirmed) return null;
  return {
    kind: "release",
    optionId: item.verification.confirmOptionId,
    message: item.verification.confirmedMessage,
    needsIdentityCheck: true,
  };
}

/** Approvals are never learned: the remember flag is forced off (§25 hard approval). */
export function rememberFlag(item: NeedsYouItem, requested: boolean): boolean {
  return item.kind === "choice" && Boolean(item.remember) && requested;
}

/** Items still open after local answers, in server order. */
export function openItems(items: readonly NeedsYouItem[], answered: ReadonlySet<string>): NeedsYouItem[] {
  return items.filter((i) => !answered.has(i.id));
}
