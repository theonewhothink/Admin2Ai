/**
 * Sample data for demo mode and for when the API cannot be reached and nothing
 * is cached. The same sample world as the web app: early October 2026, while
 * September is being closed. The screens label it as example data.
 */
import type { DecimalString } from "../lib/money";
import type { ActivityFeed, AskAnswer, HomeData, NeedsYouItem } from "./types";

const eur = (v: string) => v as DecimalString;

export const SAMPLE_TODAY = "2026-10-02";

/** No greeting: the phone greets by its own local time. */
export const sampleHome: HomeData = {
  needsYouCount: 2,
  currentMonth: { key: "2026-09", label: "September", percentClosed: 94 },
  handledPeriodLabel: "Today",
  handledToday: 11,
  handled: [
    { id: "h_docs", count: 6, label: "documents collected" },
    { id: "h_missing", count: 2, label: "missing invoices recovered" },
    { id: "h_supplier", count: 2, label: "supplier emails handled" },
    { id: "h_accountant", count: 1, label: "accountant question answered" },
  ],
  companies: [
    { id: "hazel-tree", name: "Hazel Tree", tone: "good", statusLabel: "On track", detail: "September · 94% closed" },
    { id: "company-b", name: "Company B", tone: "good", statusLabel: "Closed", detail: "September closed on 1 October" },
    {
      id: "company-c",
      name: "Company C",
      tone: "attention",
      statusLabel: "Needs one answer",
      detail: "September · 81% closed",
      pendingItemIds: ["nd_ikea_418"],
    },
  ],
  connections: [
    { id: "gmail", name: "Gmail", kind: "email", account: "laura@hazeltree.es", status: "healthy", lastSyncedAt: "2026-10-02T09:12:00+02:00" },
    { id: "caixabank", name: "CaixaBank", kind: "bank", account: "Hazel Tree · Company C", status: "healthy", lastSyncedAt: "2026-10-02T08:55:00+02:00" },
    { id: "bbva", name: "BBVA", kind: "bank", account: "Company B", status: "healthy", lastSyncedAt: "2026-10-02T08:55:00+02:00" },
  ],
};

export const sampleNeedsYou: NeedsYouItem[] = [
  {
    id: "nd_ikea_418",
    kind: "choice",
    tone: "attention",
    eyebrow: "We need one answer",
    merchant: "IKEA",
    amount: eur("418.00"),
    currency: "EUR",
    date: "2026-09-29",
    companyId: "company-c",
    paidWith: "card •••• 4817",
    question: "We found the payment but cannot tell whether it belongs to:",
    options: [
      { id: "hazel-tree", label: "Hazel Tree" },
      { id: "personal", label: "Personal" },
      {
        id: "another-company",
        label: "Another company",
        choices: [
          { id: "company-b", label: "Company B" },
          { id: "company-c", label: "Company C" },
        ],
      },
    ],
    why: [
      "It was paid with card •••• 4817, which you use for more than one company.",
      "The delivery address on the IKEA receipt is the Hazel Tree studio.",
      "Your last two IKEA orders went to Hazel Tree, but this one is larger than usual.",
    ],
    remember: {
      template: "Always use {choice} for IKEA paid with card •••• 4817",
      overrides: { personal: "Always treat IKEA paid with card •••• 4817 as personal" },
      defaultChecked: true,
    },
  },
  {
    id: "nd_vodafone_iban",
    kind: "approval",
    tone: "risk",
    eyebrow: "Payment on hold",
    merchant: "Vodafone",
    title: "Vodafone changed the IBAN shown on its invoice.",
    amount: eur("92.40"),
    currency: "EUR",
    date: "2026-10-01",
    companyId: "hazel-tree",
    body: "The bank account on October's invoice is different from the one you have paid for three years. I have blocked the payment until you confirm it is really Vodafone.",
    facts: [
      { label: "Paid until now", value: "ES76 2100 •••• •••• 4402" },
      { label: "On the new invoice", value: "LT61 3250 •••• •••• 1187", tone: "risk" },
    ],
    why: [
      "Vodafone's bank details have not changed in 36 monthly invoices.",
      "The new account is in another country.",
      "Changed bank details are a common way invoice fraud happens. I never release these without you.",
    ],
    verification: {
      optionLabel: "Confirm with Vodafone by phone",
      instruction:
        "Call Vodafone Business on 1443. That number comes from your earlier invoices, not the new one. Ask them to confirm the account ending in 1187.",
      checkboxLabel: "I called and Vodafone confirmed the account ending in 1187.",
      confirmLabel: "They confirmed it. Release the payment.",
      confirmOptionId: "confirmed_by_phone",
      confirmedMessage: "Done. The payment will go to the new account.",
    },
    keepBlocked: {
      label: "Keep blocked",
      optionId: "keep_blocked",
      message: "Done. It stays blocked. I will ask Vodafone for a corrected invoice.",
    },
  },
];

export const sampleActivity: ActivityFeed = {
  today: SAMPLE_TODAY,
  items: [
    { id: "a01", at: "2026-10-02T09:12:00+02:00", kind: "collected", text: "Collected the Vodafone invoice from your email.", companyName: "Hazel Tree", amount: eur("92.40"), currency: "EUR" },
    { id: "a02", at: "2026-10-02T08:47:00+02:00", kind: "protected", text: "Put the Vodafone payment on hold. The bank details on the invoice changed.", companyName: "Hazel Tree" },
    { id: "a03", at: "2026-10-02T08:30:00+02:00", kind: "recovered", text: "Recovered the missing Adobe invoice from an email Jorge forwarded.", companyName: "Company C", amount: eur("59.99"), currency: "EUR" },
    { id: "a04", at: "2026-10-02T07:02:00+02:00", kind: "checked", text: "Checked 23 new bank transactions. All of them matched.", companyName: "Company B" },
    { id: "a05", at: "2026-10-01T18:20:00+02:00", kind: "answered", text: "Answered your accountant: the €1,200 transfer to M. García is September's office rent.", companyName: "Hazel Tree" },
    { id: "a06", at: "2026-10-01T16:05:00+02:00", kind: "chased", text: "Asked Endesa for the September electricity invoice.", companyName: "Hazel Tree" },
    { id: "a07", at: "2026-10-01T11:10:00+02:00", kind: "closed", text: "Closed September. Nothing is left open.", companyName: "Company B" },
    { id: "a08", at: "2026-09-30T17:48:00+02:00", kind: "recovered", text: "Recovered the missing Iberia invoice for the Lisbon trip.", companyName: "Company C", amount: eur("236.80"), currency: "EUR" },
    { id: "a09", at: "2026-09-29T10:05:00+02:00", kind: "learned", text: "Learned that Mercadona on card •••• 2210 is personal. I will not ask again." },
  ],
};

export const sampleAskAnswers: Readonly<Record<string, AskAnswer>> = {
  "Is September complete?": {
    answer:
      "Almost. Company B is closed. Hazel Tree is 94% done: two supplier invoices are on their way. Company C needs one answer from you about an IKEA payment.",
    evidence: [
      { label: "Company B · September closed", id: "month:company-b:2026-09" },
      { label: "Hazel Tree · September 94%", id: "month:hazel-tree:2026-09" },
      { label: "IKEA · €418.00", id: "needs:nd_ikea_418" },
    ],
  },
  "Did we pay Vodafone?": {
    answer:
      "For September, yes: €92.40 on 2 September, matched to invoice VF-2609-1183. October's payment is on hold because the bank details on the new invoice changed. I need you to confirm them.",
    evidence: [
      { label: "Invoice VF-2609-1183 · €92.40", id: "doc:vf-2609-1183" },
      { label: "Direct debit 2 Sep · €92.40", id: "txn:caixa-0902-vodafone" },
      { label: "October payment on hold", id: "needs:nd_vodafone_iban" },
    ],
  },
  "What still needs my attention?": {
    answer:
      "Two things. An IKEA payment of €418.00: I need to know which company it belongs to. And Vodafone's October payment is on hold until you confirm their new bank details.",
    evidence: [
      { label: "IKEA · €418.00", id: "needs:nd_ikea_418" },
      { label: "Vodafone · payment on hold", id: "needs:nd_vodafone_iban" },
    ],
  },
  "Show subscriptions that increased.": {
    answer:
      "Three went up in the last three months. Adobe Creative Cloud: €54.99 → €59.99. Google Workspace: €69.30 → €83.21, after you added two people. Notion: €8.00 → €10.00.",
    evidence: [
      { label: "Adobe · €59.99", id: "doc:adobe-2026-09" },
      { label: "Google Workspace · €83.21", id: "doc:gws-2026-09" },
      { label: "Notion · €10.00", id: "doc:notion-2026-09" },
    ],
  },
};

export const sampleAskFallback: AskAnswer = {
  answer: "I could not find a clear answer to that yet. Try asking about a supplier, a payment or a month, for example “Did we pay Vodafone?”",
  evidence: [],
};

/** Keyword routing for demo answers. Only ever used on sample data. */
export function sampleAnswer(question: string): AskAnswer {
  const q = question.trim().toLowerCase().replace(/[?.!]+$/, "");
  const exact = Object.entries(sampleAskAnswers).find(([k]) => k.toLowerCase().replace(/[?.!]+$/, "") === q);
  if (exact) return exact[1];
  const pick = (key: string) => sampleAskAnswers[key] ?? sampleAskFallback;
  if (q.includes("vodafone")) return pick("Did we pay Vodafone?");
  if (q.includes("subscription") || q.includes("increase") || q.includes("went up")) return pick("Show subscriptions that increased.");
  if (q.includes("attention") || q.includes("need") || q.includes("to do")) return pick("What still needs my attention?");
  if (q.includes("september") || q.includes("complete") || q.includes("closed")) return pick("Is September complete?");
  return sampleAskFallback;
}
