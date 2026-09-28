/**
 * Sample data used whenever the backend is not reachable.
 * The sample world is set in early October 2026, while September is being closed.
 */
import type {
  AccountantClientDetail,
  AccountantClientRow,
  ActivityFeed,
  AskAnswer,
  AuditResult,
  CompanyLookup,
  CompanySummary,
  Connection,
  HomeData,
  LearningCounter,
  MatchedItem,
  MonthClose,
  MonthKey,
  NeedsYouItem,
  OneTapQuestion,
  Owner,
  SourcesData,
} from "./types";

export const SAMPLE_TODAY = "2026-10-02";

export const owner: Owner = {
  firstName: "Laura",
  fullName: "Laura Medina",
  email: "laura@hazeltree.es",
  initials: "LM",
};

/* ---------- Companies ---------- */

export const companies: CompanySummary[] = [
  {
    id: "hazel-tree",
    name: "Hazel Tree",
    legalName: "Hazel Tree Interiors S.L.",
    taxId: "B67284519",
    tone: "good",
    statusLabel: "On track",
    detail: "September · 94% closed",
    currentMonth: "2026-09",
    months: ["2026-09", "2026-08", "2026-07"],
  },
  {
    id: "company-b",
    name: "Company B",
    legalName: "Company B S.L.",
    taxId: "B09876543",
    tone: "good",
    statusLabel: "Closed",
    detail: "September closed on 1 October",
    currentMonth: "2026-09",
    months: ["2026-09", "2026-08", "2026-07"],
  },
  {
    id: "company-c",
    name: "Company C",
    legalName: "Company C Studio S.L.",
    taxId: "B55120987",
    tone: "attention",
    statusLabel: "Needs one answer",
    detail: "September · 81% closed",
    currentMonth: "2026-09",
    months: ["2026-09", "2026-08"],
    pendingItemIds: ["nd_ikea_418"],
  },
];

/* ---------- Connections ---------- */

export const connections: Connection[] = [
  {
    id: "gmail",
    name: "Gmail",
    kind: "email",
    account: "laura@hazeltree.es",
    status: "healthy",
    lastSyncedAt: "2026-10-02T09:12:00+02:00",
  },
  {
    id: "caixabank",
    name: "CaixaBank",
    kind: "bank",
    account: "Hazel Tree · Company C",
    status: "healthy",
    lastSyncedAt: "2026-10-02T08:55:00+02:00",
  },
  {
    id: "bbva",
    name: "BBVA",
    kind: "bank",
    account: "Company B",
    status: "healthy",
    lastSyncedAt: "2026-10-02T08:55:00+02:00",
  },
  {
    id: "accountant",
    name: "Asesoría Vidal",
    kind: "accountant",
    account: "marc@asesoriavidal.es",
    status: "healthy",
    lastSyncedAt: "2026-10-01T18:20:00+02:00",
  },
];

/** Connections as they look when Gmail has stopped syncing (`?demo=stale`). */
export const staleConnections: Connection[] = connections.map((c) =>
  c.id === "gmail"
    ? {
        ...c,
        status: "stale",
        lastSyncedAt: "2026-10-01T14:42:00+02:00",
        lastSyncedLabel: "14:42 yesterday",
      }
    : c,
);

/* ---------- Home ---------- */

export const home: HomeData = {
  greeting: "Good morning.",
  needsYouCount: 2,
  dueSoon: [
    {
      id: "due_vodafone",
      title: "Vodafone payment",
      companyName: "Hazel Tree",
      due: "2026-10-05",
      note: "On hold until you confirm the new bank details.",
      tone: "risk",
      href: "/needs-you#nd_vodafone_iban",
    },
    {
      id: "due_vat_ht",
      title: "Quarterly VAT return",
      companyName: "Hazel Tree",
      due: "2026-10-20",
      note: "Figures are ready. Your accountant files it.",
      tone: "good",
    },
    {
      id: "due_withholding_b",
      title: "Quarterly withholding tax",
      companyName: "Company B",
      due: "2026-10-20",
      note: "Prepared and sent to your accountant.",
      tone: "good",
    },
  ],
  currentMonth: { key: "2026-09", label: "September", percentClosed: 94 },
  companies,
  handledPeriodLabel: "This week",
  handled: [
    { id: "h_docs", count: 14, label: "documents collected" },
    { id: "h_missing", count: 3, label: "missing invoices recovered" },
    { id: "h_supplier", count: 2, label: "supplier emails handled" },
    { id: "h_accountant", count: 1, label: "accountant question answered" },
  ],
  connections,
};

/* ---------- Needs you ---------- */

export const needsYou: NeedsYouItem[] = [
  {
    id: "nd_ikea_418",
    kind: "choice",
    tone: "attention",
    eyebrow: "We need one answer",
    merchant: "IKEA",
    amount: 418,
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
    amount: 92.4,
    currency: "EUR",
    date: "2026-10-01",
    companyId: "hazel-tree",
    body: "The bank account on October’s invoice is different from the one you have paid for three years. I have blocked the payment until you confirm it is really Vodafone.",
    facts: [
      { label: "Paid until now", value: "ES76 2100 •••• •••• 4402" },
      { label: "On the new invoice", value: "LT61 3250 •••• •••• 1187", tone: "risk" },
    ],
    why: [
      "Vodafone’s bank details have not changed in 36 monthly invoices.",
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

/* ---------- Activity ---------- */

export const activity: ActivityFeed = {
  today: SAMPLE_TODAY,
  items: [
    { id: "a01", at: "2026-10-02T09:12:00+02:00", kind: "collected", text: "Collected the Vodafone invoice from your email.", companyName: "Hazel Tree", amount: 92.4, currency: "EUR" },
    { id: "a02", at: "2026-10-02T08:47:00+02:00", kind: "protected", text: "Put the Vodafone payment on hold. The bank details on the invoice changed.", companyName: "Hazel Tree" },
    { id: "a03", at: "2026-10-02T08:30:00+02:00", kind: "recovered", text: "Recovered the missing Adobe invoice from an email Jorge forwarded.", companyName: "Company C", amount: 59.99, currency: "EUR" },
    { id: "a04", at: "2026-10-02T07:02:00+02:00", kind: "checked", text: "Checked 23 new bank transactions. All of them matched.", companyName: "Company B" },
    { id: "a05", at: "2026-10-01T18:20:00+02:00", kind: "answered", text: "Answered your accountant: the €1,200 transfer to M. García is September’s office rent.", companyName: "Hazel Tree" },
    { id: "a06", at: "2026-10-01T16:05:00+02:00", kind: "chased", text: "Asked Endesa for the September electricity invoice.", companyName: "Hazel Tree" },
    { id: "a07", at: "2026-10-01T11:10:00+02:00", kind: "closed", text: "Closed September. Nothing is left open.", companyName: "Company B" },
    { id: "a08", at: "2026-10-01T10:31:00+02:00", kind: "collected", text: "Collected 6 invoices from your email.", companyName: "Hazel Tree" },
    { id: "a09", at: "2026-09-30T17:48:00+02:00", kind: "recovered", text: "Recovered the missing Iberia invoice for the Lisbon trip.", companyName: "Company C", amount: 236.8, currency: "EUR" },
    { id: "a10", at: "2026-09-30T12:14:00+02:00", kind: "chased", text: "Replied to Studio Nord with your new billing address. They sent the corrected invoice.", companyName: "Hazel Tree" },
    { id: "a11", at: "2026-09-30T09:02:00+02:00", kind: "collected", text: "Collected 5 receipts from your email.", companyName: "Company C" },
    { id: "a12", at: "2026-09-29T19:40:00+02:00", kind: "recovered", text: "Recovered the missing Google Workspace invoice from the admin console.", companyName: "Hazel Tree", amount: 83.21, currency: "EUR" },
    { id: "a13", at: "2026-09-29T15:22:00+02:00", kind: "chased", text: "Reminded Papelería Soler to send their September invoice.", companyName: "Company B" },
    { id: "a14", at: "2026-09-29T10:05:00+02:00", kind: "learned", text: "Learned that Mercadona on card •••• 2210 is personal. I will not ask again.", companyName: undefined },
    { id: "a15", at: "2026-09-29T08:40:00+02:00", kind: "collected", text: "Collected 2 invoices from supplier portals.", companyName: "Company B" },
  ],
};

/* ---------- Months ---------- */

const googleWorkspace: MatchedItem = {
  id: "m_gws",
  supplier: "Google Workspace",
  description: "Monthly subscription",
  amount: 83.21,
  currency: "EUR",
  date: "2026-09-03",
  reasons: [
    "Invoice total €83.21",
    "Bank charge €83.21",
    "Dates 1 day apart",
    "Supplier VAT matches",
    "Card ending matches",
    "Historical pattern: monthly",
  ],
};

const vodafoneSept: MatchedItem = {
  id: "m_vodafone",
  supplier: "Vodafone",
  description: "Phone and internet",
  amount: 92.4,
  currency: "EUR",
  date: "2026-09-02",
  reasons: [
    "Invoice total €92.40",
    "Direct debit €92.40",
    "Same day",
    "Supplier VAT matches",
    "Direct debit reference matches",
    "Historical pattern: monthly",
  ],
};

const rent: MatchedItem = {
  id: "m_rent",
  supplier: "M. García",
  description: "Studio rent",
  amount: 1200,
  currency: "EUR",
  date: "2026-09-01",
  reasons: [
    "Rent contract on file",
    "Transfer €1,200.00",
    "Reference says “Alquiler septiembre”",
    "Paid on the 1st, as every month since January 2024",
  ],
};

const studioNord: MatchedItem = {
  id: "m_studio_nord",
  supplier: "Studio Nord",
  description: "Design work, invoice SN-2026-041",
  amount: 800,
  currency: "EUR",
  date: "2026-09-29",
  reasons: [
    "Invoice total €800.00",
    "Transfer €800.00",
    "IBAN matches the invoice",
    "Invoice number in the transfer reference",
  ],
};

const repsol: MatchedItem = {
  id: "m_repsol",
  supplier: "Repsol",
  description: "Fuel",
  amount: 64.1,
  currency: "EUR",
  date: "2026-09-18",
  reasons: [
    "Receipt photo total €64.10",
    "Card charge €64.10",
    "Same day",
    "Card ending matches",
  ],
};

const iberia: MatchedItem = {
  id: "m_iberia",
  supplier: "Iberia",
  description: "Flights to Lisbon",
  amount: 236.8,
  currency: "EUR",
  date: "2026-09-12",
  reasons: [
    "Invoice total €236.80",
    "Card charge €236.80",
    "Dates 2 days apart",
    "Passenger name matches",
    "Card ending matches",
  ],
};

const baseStats = {
  transactionsChecked: 0,
  documentsCollected: 0,
  missingDocumentsRetrieved: 0,
  suppliersChased: 0,
  accountantQuestionsResolved: 0,
  taxObligationsVerified: 0,
  unresolvedIssues: 0,
  minutesSpent: 0,
};

export const months: MonthClose[] = [
  {
    companyId: "company-b",
    month: "2026-09",
    status: "closed",
    percentClosed: 100,
    transactionsTotal: 218,
    closedOn: "2026-10-01",
    stats: {
      transactionsChecked: 218,
      documentsCollected: 186,
      missingDocumentsRetrieved: 14,
      suppliersChased: 7,
      accountantQuestionsResolved: 3,
      taxObligationsVerified: 2,
      unresolvedIssues: 0,
      minutesSpent: 4,
    },
    remaining: [],
    matched: [googleWorkspace, repsol, { ...rent, id: "m_rent_b", supplier: "Oficinas Diagonal", description: "Office rent", reasons: ["Rent contract on file", "Transfer €1,200.00", "Reference says “Renta oficina 09”", "Paid on the 1st, every month"] }],
  },
  {
    companyId: "company-b",
    month: "2026-08",
    status: "closed",
    percentClosed: 100,
    transactionsTotal: 171,
    closedOn: "2026-09-03",
    stats: { ...baseStats, transactionsChecked: 171, documentsCollected: 149, missingDocumentsRetrieved: 9, suppliersChased: 4, accountantQuestionsResolved: 1, taxObligationsVerified: 1, minutesSpent: 3 },
    remaining: [],
    matched: [{ ...googleWorkspace, date: "2026-08-03" }, { ...repsol, date: "2026-08-21", amount: 58.3, reasons: ["Receipt photo total €58.30", "Card charge €58.30", "Same day", "Card ending matches"] }],
  },
  {
    companyId: "company-b",
    month: "2026-07",
    status: "closed",
    percentClosed: 100,
    transactionsTotal: 194,
    closedOn: "2026-08-04",
    stats: { ...baseStats, transactionsChecked: 194, documentsCollected: 170, missingDocumentsRetrieved: 11, suppliersChased: 5, accountantQuestionsResolved: 2, taxObligationsVerified: 2, minutesSpent: 6 },
    remaining: [],
    matched: [{ ...googleWorkspace, date: "2026-07-03" }],
  },
  {
    companyId: "hazel-tree",
    month: "2026-09",
    status: "open",
    percentClosed: 94,
    transactionsTotal: 174,
    stats: { ...baseStats, transactionsChecked: 164, documentsCollected: 141, missingDocumentsRetrieved: 6, suppliersChased: 3, accountantQuestionsResolved: 1, taxObligationsVerified: 1, minutesSpent: 2 },
    remaining: [
      { id: "r1", text: "Two supplier invoices are on their way. I asked Endesa and Adobe yesterday; both usually reply within a day.", tone: "neutral" },
      { id: "r2", text: "Eight small card payments are waiting for their receipts. I will match them tonight when your email syncs.", tone: "neutral" },
      { id: "r3", text: "Once those arrive, I will send the month to your accountant.", tone: "neutral" },
    ],
    notices: [
      { id: "n1", text: "October’s Vodafone payment is on hold. The bank details on the invoice changed.", tone: "risk", href: "/needs-you#nd_vodafone_iban", linkLabel: "Review" },
    ],
    matched: [googleWorkspace, vodafoneSept, rent, studioNord],
  },
  {
    companyId: "hazel-tree",
    month: "2026-08",
    status: "closed",
    percentClosed: 100,
    transactionsTotal: 158,
    closedOn: "2026-09-04",
    stats: { ...baseStats, transactionsChecked: 158, documentsCollected: 139, missingDocumentsRetrieved: 12, suppliersChased: 6, accountantQuestionsResolved: 2, taxObligationsVerified: 1, minutesSpent: 5 },
    remaining: [],
    matched: [{ ...googleWorkspace, date: "2026-08-03" }, { ...vodafoneSept, date: "2026-08-02" }, { ...rent, date: "2026-08-01", reasons: ["Rent contract on file", "Transfer €1,200.00", "Reference says “Alquiler agosto”", "Paid on the 1st, as every month since January 2024"] }],
  },
  {
    companyId: "hazel-tree",
    month: "2026-07",
    status: "closed",
    percentClosed: 100,
    transactionsTotal: 181,
    closedOn: "2026-08-05",
    stats: { ...baseStats, transactionsChecked: 181, documentsCollected: 162, missingDocumentsRetrieved: 15, suppliersChased: 8, accountantQuestionsResolved: 3, taxObligationsVerified: 2, minutesSpent: 7 },
    remaining: [],
    matched: [{ ...googleWorkspace, date: "2026-07-03" }, { ...vodafoneSept, date: "2026-07-02" }],
  },
  {
    companyId: "company-c",
    month: "2026-09",
    status: "open",
    percentClosed: 81,
    transactionsTotal: 96,
    stats: { ...baseStats, transactionsChecked: 78, documentsCollected: 64, missingDocumentsRetrieved: 4, suppliersChased: 2, minutesSpent: 1 },
    remaining: [
      { id: "r1", text: "I still need one thing from you: which company the IKEA payment of €418.00 belongs to.", tone: "attention", href: "/needs-you#nd_ikea_418", linkLabel: "Answer" },
      { id: "r2", text: "Three receipts from the Lisbon trip are missing. I emailed Jorge this morning.", tone: "neutral" },
      { id: "r3", text: "Fourteen payments are being matched with invoices that arrived today.", tone: "neutral" },
    ],
    matched: [iberia, { ...googleWorkspace, id: "m_gws_c", amount: 13.8, reasons: ["Invoice total €13.80", "Bank charge €13.80", "Dates 1 day apart", "Supplier VAT matches", "Card ending matches", "Historical pattern: monthly"] }],
  },
  {
    companyId: "company-c",
    month: "2026-08",
    status: "closed",
    percentClosed: 100,
    transactionsTotal: 88,
    closedOn: "2026-09-06",
    stats: { ...baseStats, transactionsChecked: 88, documentsCollected: 79, missingDocumentsRetrieved: 5, suppliersChased: 2, accountantQuestionsResolved: 1, taxObligationsVerified: 1, minutesSpent: 3 },
    remaining: [],
    matched: [{ ...iberia, id: "m_iberia_aug", date: "2026-08-08", amount: 189.4, description: "Flights to Madrid", reasons: ["Invoice total €189.40", "Card charge €189.40", "Same day", "Passenger name matches", "Card ending matches"] }],
  },
];

export function findMonth(companyId: string, month: MonthKey): MonthClose | null {
  return months.find((m) => m.companyId === companyId && m.month === month) ?? null;
}

/* ---------- Ask ---------- */

export const askExamples: string[] = [
  "Is September complete?",
  "Did we pay Vodafone?",
  "Find the invoice for the €800 payment yesterday.",
  "What still needs my attention?",
  "What did the accountant ask this month?",
  "Show subscriptions that increased.",
];

export const askAnswers: Record<string, AskAnswer> = {
  "Is September complete?": {
    answer:
      "Almost. Company B is closed. Hazel Tree is 94% done — two supplier invoices are on their way. Company C needs one answer from you about an IKEA payment.",
    evidence: [
      { label: "Company B · September closed", id: "month:company-b:2026-09" },
      { label: "Hazel Tree · September 94%", id: "month:hazel-tree:2026-09" },
      { label: "IKEA · €418.00", id: "needs:nd_ikea_418" },
    ],
  },
  "Did we pay Vodafone?": {
    answer:
      "For September, yes: €92.40 on 2 September, matched to invoice VF-2609-1183. October’s payment is on hold because the bank details on the new invoice changed. I need you to confirm them.",
    evidence: [
      { label: "Invoice VF-2609-1183 · €92.40", id: "doc:vf-2609-1183" },
      { label: "Direct debit 2 Sep · €92.40", id: "txn:caixa-0902-vodafone" },
      { label: "October payment on hold", id: "needs:nd_vodafone_iban" },
    ],
  },
  "Find the invoice for the €800 payment yesterday.": {
    answer:
      "The €800.00 transfer went to Studio Nord. Their invoice SN-2026-041 arrived by email on 29 September. The amount, the IBAN and the invoice number all match.",
    evidence: [
      { label: "Invoice SN-2026-041 · €800.00", id: "doc:sn-2026-041" },
      { label: "Transfer · €800.00", id: "txn:caixa-1001-studio-nord" },
      { label: "Hazel Tree · September", id: "month:hazel-tree:2026-09" },
    ],
  },
  "What still needs my attention?": {
    answer:
      "Two things. An IKEA payment of €418.00 — I need to know which company it belongs to. And Vodafone’s October payment is on hold until you confirm their new bank details.",
    evidence: [
      { label: "IKEA · €418.00", id: "needs:nd_ikea_418" },
      { label: "Vodafone · payment on hold", id: "needs:nd_vodafone_iban" },
    ],
  },
  "What did the accountant ask this month?": {
    answer:
      "Three questions, all answered. Whether the €1,200 transfer to M. García is rent — yes, I answered from the contract. Whether the Lisbon trip was for Company C — you confirmed it. And for the quarter’s sales list, which I sent on 30 September.",
    evidence: [
      { label: "Rent contract · M. García", id: "doc:rent-contract-garcia" },
      { label: "Lisbon trip · Company C", id: "month:company-c:2026-09" },
      { label: "Sales list · Q3", id: "doc:sales-list-q3" },
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

export const askFallback: AskAnswer = {
  answer:
    "I could not find a clear answer to that yet. Try asking about a supplier, a payment, or a month — for example, “Did we pay Vodafone?”",
  evidence: [],
};

/* ---------- Free audit ---------- */

export const audit: AuditResult = {
  companyName: "Hazel Tree",
  periodLabel: "the last 12 months",
  findings: [
    { id: "f_expenses", value: "€38,412", label: "of expenses", tone: "neutral", examples: ["Largest supplier: Studio Nord, €9,600", "Rent: €14,400", "Software: €2,184"] },
    { id: "f_docs", value: "138", label: "documents", tone: "neutral", examples: ["112 invoices in your email", "19 receipts in photo form", "7 downloaded from supplier portals"] },
    { id: "f_subs", value: "12", label: "recurring subscriptions", tone: "neutral", examples: ["Google Workspace, Adobe, Notion, Vodafone, Holded and 7 more"] },
    { id: "f_missing", value: "7", label: "transactions apparently missing evidence", tone: "attention", examples: ["Repsol €64.10 on 18 September", "Amazon €37.90 on 2 September", "5 more under €40"] },
    { id: "f_increased", value: "3", label: "subscriptions increased", tone: "attention", examples: ["Adobe €54.99 → €59.99", "Google Workspace €69.30 → €83.21", "Notion €8.00 → €10.00"] },
    { id: "f_other", value: "4", label: "items may belong to another company", tone: "attention", examples: ["IKEA €418.00 paid with card •••• 4817", "3 restaurant bills in Lisbon"] },
  ],
};

/* ---------- Onboarding ---------- */

export const companyLookup: CompanyLookup = {
  legalName: "Hazel Tree Interiors S.L.",
  taxId: "B67284519",
  address: "Carrer de Mallorca 214, 08008 Barcelona",
  activity: "Interior design",
  registeredSince: "VAT registered since March 2019",
};

export const learningCounters: LearningCounter[] = [
  { id: "c_email", source: "Email", value: 12482, label: "messages analyzed" },
  { id: "c_docs", source: "Documents", value: 638, label: "found" },
  { id: "c_bank", source: "Bank", value: 427, label: "transactions" },
  { id: "c_suppliers", source: "Suppliers", value: 73, label: "recognized" },
  { id: "c_recurring", source: "Recurring expenses", value: 18, label: "found" },
  { id: "c_missing", source: "Possible missing documents", value: 11, label: "to look for" },
  { id: "c_entities", source: "Entities", value: 1, label: "company" },
];

export const learningUnderstood = 96;

export const oneTapQuestions: OneTapQuestion[] = [
  {
    id: "q_vodafone",
    subject: "Vodafone",
    amount: 92.4,
    currency: "EUR",
    cadence: "monthly",
    options: [
      { id: "company", label: "Company telecom" },
      { id: "personal", label: "Personal" },
      { id: "other", label: "Other" },
    ],
  },
  {
    id: "q_adobe",
    subject: "Adobe",
    amount: 59.99,
    currency: "EUR",
    cadence: "monthly",
    options: [
      { id: "company", label: "Company software" },
      { id: "personal", label: "Personal" },
      { id: "other", label: "Other" },
    ],
  },
  {
    id: "q_garcia",
    subject: "Transfer to M. García",
    amount: 1200,
    currency: "EUR",
    cadence: "monthly",
    options: [
      { id: "rent", label: "Studio rent" },
      { id: "personal", label: "Personal" },
      { id: "other", label: "Other" },
    ],
  },
  {
    id: "q_amazon",
    subject: "Amazon on card •••• 4817",
    amount: 37.9,
    currency: "EUR",
    cadence: "a few times a month",
    options: [
      { id: "company", label: "Usually company" },
      { id: "personal", label: "Usually personal" },
      { id: "ask", label: "Ask me each time" },
    ],
  },
];

/* ---------- Accountant workspace ---------- */

export const accountantFirm = { name: "Asesoría Vidal", person: "Marc Vidal", initials: "MV" };

export const accountantClients: AccountantClientRow[] = [
  { id: "company-c", name: "Company C Studio S.L.", month: "September", complete: 81, missing: 3, needsAccountant: 2 },
  { id: "marta-ruiz", name: "Marta Ruiz (freelance)", month: "September", complete: 72, missing: 9, needsAccountant: 1 },
  { id: "lumen-dental", name: "Lumen Dental S.L.P.", month: "September", complete: 88, missing: 4, needsAccountant: 3 },
  { id: "hazel-tree", name: "Hazel Tree Interiors S.L.", month: "September", complete: 94, missing: 2, needsAccountant: 1 },
  { id: "casa-oliva", name: "Casa Oliva S.L.", month: "September", complete: 97, missing: 1, needsAccountant: 0 },
  { id: "company-b", name: "Company B S.L.", month: "September", complete: 100, missing: 0, needsAccountant: 0 },
];

const hazelTreeDetail: AccountantClientDetail = {
  id: "hazel-tree",
  name: "Hazel Tree Interiors S.L.",
  month: "September",
  complete: 94,
  missing: 2,
  needsAccountant: 1,
  taxId: "B67284519",
  software: "A3",
  evidence: [
    { label: "Transactions", value: "174" },
    { label: "Matched with a document", value: "164" },
    { label: "Documents collected", value: "141" },
    { label: "Still missing", value: "2" },
  ],
  anomalies: [
    { id: "an1", title: "Vodafone bank details changed", detail: "October payment blocked until the owner confirms by phone.", tone: "risk" },
    { id: "an2", title: "Adobe price went up 9%", detail: "€54.99 → €59.99 from September. Same plan.", tone: "attention" },
  ],
  taxFlags: [
    { id: "t1", title: "Restaurant bill on a Saturday · €142.60", detail: "Client says it was a client lunch. Deductibility is your call." },
    { id: "t2", title: "Studio Nord invoice without withholding", detail: "Freelance supplier, 15% withholding may apply." },
  ],
  questions: [
    { id: "q1", question: "Is the €1,200 transfer to M. García the studio rent?", status: "answered", answer: "Yes. Matched to the rent contract on file." },
    { id: "q2", question: "Can you confirm the Studio Nord invoice is for design work?", status: "waiting" },
  ],
  exportState: { state: "partial", ready: 164, total: 174, note: "164 of 174 transactions are ready for A3. The rest will follow when the last invoices arrive." },
};

const clientTaxIds: Record<string, string> = {
  "company-b": "B09876543",
  "company-c": "B55120987",
  "lumen-dental": "B66431208",
  "casa-oliva": "B25877410",
  "marta-ruiz": "46218853K",
};

export function accountantClientDetail(id: string): AccountantClientDetail | null {
  if (id === "hazel-tree") return hazelTreeDetail;
  const row = accountantClients.find((c) => c.id === id);
  if (!row) return null;
  const ready = row.complete === 100;
  return {
    ...row,
    taxId: clientTaxIds[row.id] ?? "—",
    software: "A3",
    evidence: [
      { label: "Completion", value: `${row.complete}%` },
      { label: "Still missing", value: String(row.missing) },
      { label: "Waiting for you", value: String(row.needsAccountant) },
    ],
    anomalies: row.missing > 3
      ? [{ id: "an1", title: "Several receipts are late", detail: `${row.missing} card payments have no receipt yet. The client has been reminded.`, tone: "attention" }]
      : [],
    taxFlags: row.needsAccountant > 0
      ? [{ id: "t1", title: "Mixed personal and business card use", detail: "Some card payments may be personal. The client is confirming." }]
      : [],
    questions: row.needsAccountant > 0
      ? [{ id: "q1", question: "Please confirm the treatment of the flagged card payments.", status: "waiting" }]
      : [],
    exportState: ready
      ? { state: "exported", ready: 1, total: 1, note: "September was exported to A3 on 1 October." }
      : { state: "partial", ready: row.complete, total: 100, note: `${row.complete}% of the month is ready to export.` },
  };
}

/** Connected sources and what I have learned (mirrors GET /api/sources on the demo data). */
export const sources: SourcesData = {
  "groups": [
    {
      "id": "email",
      "title": "Email",
      "description": "Where invoices, letters and receipts arrive.",
      "items": [
        {
          "id": "gmail",
          "name": "laura@hazeltree.pt",
          "company": "All companies",
          "detail": "Gmail",
          "status": "healthy",
          "lastSyncedAt": "2026-10-02T09:12:00+01:00"
        }
      ]
    },
    {
      "id": "banks",
      "title": "Bank accounts",
      "description": "Every payment in and out is checked against evidence.",
      "items": [
        {
          "id": "mbcp-ht",
          "name": "Millennium BCP •••• 0265",
          "company": "Hazel Tree",
          "detail": "PT50 •••• 0265",
          "status": "healthy"
        },
        {
          "id": "mbcp-cc",
          "name": "Millennium BCP •••• 3382",
          "company": "Company C",
          "detail": "PT50 •••• 3382",
          "status": "healthy"
        },
        {
          "id": "cgd-b",
          "name": "Caixa Geral de Depósitos •••• 3007",
          "company": "Company B",
          "detail": "PT50 •••• 3007",
          "status": "healthy"
        }
      ]
    },
    {
      "id": "cards",
      "title": "Cards",
      "description": "Card spending is matched to receipts.",
      "items": [
        {
          "id": "card-5530",
          "name": "Card •••• 5530",
          "company": "Hazel Tree",
          "detail": "Millennium BCP",
          "status": "healthy"
        },
        {
          "id": "card-7702",
          "name": "Card •••• 7702",
          "company": "Company B",
          "detail": "Caixa Geral de Depósitos",
          "status": "healthy"
        },
        {
          "id": "card-2291",
          "name": "Card •••• 2291",
          "company": "Company C",
          "detail": "Millennium BCP",
          "status": "healthy"
        },
        {
          "id": "card-4817",
          "name": "Card •••• 4817",
          "company": "Company C",
          "detail": "Millennium BCP · personal card used for business",
          "status": "healthy"
        }
      ]
    },
    {
      "id": "accountant",
      "title": "Accountant",
      "description": "Receives the monthly package and asks questions here.",
      "items": [
        {
          "id": "accountant",
          "name": "Contabilidade Vidal",
          "company": "All companies",
          "detail": "marc@contabilidadevidal.pt",
          "status": "healthy",
          "lastSyncedAt": "2026-10-01T18:20:00+01:00"
        }
      ]
    },
    {
      "id": "suppliers",
      "title": "Suppliers",
      "description": "Recognised from invoices and payments.",
      "items": [
        {
          "id": "sup-adobe",
          "name": "Adobe",
          "company": "Company C",
          "detail": "1 document · 1 payment",
          "lastSeen": "2026-09-22",
          "status": "known"
        },
        {
          "id": "sup-edp",
          "name": "EDP",
          "company": "Hazel Tree",
          "detail": "0 documents · 1 payment",
          "lastSeen": "2026-09-19",
          "status": "known"
        },
        {
          "id": "sup-ikea",
          "name": "IKEA",
          "company": "Company C",
          "detail": "1 document · 1 payment",
          "lastSeen": "2026-09-29",
          "status": "known"
        },
        {
          "id": "sup-landlord",
          "name": "Marta Gonçalves",
          "company": "Hazel Tree",
          "detail": "1 document · 1 payment · bank details on file",
          "lastSeen": "2026-09-01",
          "status": "known"
        },
        {
          "id": "sup-predial",
          "name": "Predial Alfama",
          "company": "Company B",
          "detail": "1 document · 1 payment · bank details on file",
          "lastSeen": "2026-09-01",
          "status": "known"
        },
        {
          "id": "sup-uber",
          "name": "Uber",
          "company": "Company B, Hazel Tree",
          "detail": "2 documents · 2 payments",
          "lastSeen": "2026-09-15",
          "status": "known"
        },
        {
          "id": "sup-vodafone",
          "name": "Vodafone",
          "company": "Hazel Tree",
          "detail": "2 documents · 1 payment · bank details on file",
          "lastSeen": "2026-09-02",
          "status": "hold"
        }
      ]
    },
    {
      "id": "insurance",
      "title": "Insurance",
      "description": "Policies found in email and payments. I watch the renewal dates.",
      "items": [
        {
          "id": "rel-fidelidade",
          "name": "Fidelidade",
          "company": "Hazel Tree",
          "detail": "Liability insurance · €38.20 a month",
          "foundIn": "Policy email and monthly direct debit",
          "renewsOn": "2027-01-15",
          "status": "known"
        },
        {
          "id": "rel-allianz",
          "name": "Allianz",
          "company": "Company B",
          "detail": "Commercial property insurance · €612.00 a year",
          "foundIn": "Renewal letter in email",
          "renewsOn": "2026-11-30",
          "status": "known"
        },
        {
          "id": "rel-ageas",
          "name": "Ageas",
          "company": "Company C",
          "detail": "Health insurance for 2 people · €74.80 a month",
          "foundIn": "Monthly direct debit",
          "renewsOn": "2027-03-01",
          "status": "known"
        }
      ]
    },
    {
      "id": "investments",
      "title": "Investments",
      "description": "Holdings and regular contributions.",
      "items": [
        {
          "id": "rel-spv",
          "name": "Alfama Property SPV",
          "company": "Company C",
          "detail": "100% owned · holds the studio lease",
          "foundIn": "Shareholder resolution in email",
          "renewsOn": null,
          "status": "known"
        },
        {
          "id": "rel-fund",
          "name": "Indexa Global Equity Fund",
          "company": "Hazel Tree",
          "detail": "Monthly contribution of €500.00",
          "foundIn": "Bank transfers and fund statements",
          "renewsOn": null,
          "status": "known"
        },
        {
          "id": "rel-ppr",
          "name": "Retirement savings plan (PPR)",
          "company": "Company B",
          "detail": "Quarterly contribution of €750.00",
          "foundIn": "Bank transfers",
          "renewsOn": null,
          "status": "known"
        }
      ]
    },
    {
      "id": "lenders",
      "title": "Loans",
      "description": "Repayments are matched to loan statements.",
      "items": [
        {
          "id": "rel-loan",
          "name": "Millennium BCP loan",
          "company": "Hazel Tree",
          "detail": "Equipment loan · €310.00 a month until 2028",
          "foundIn": "Loan statement and direct debit",
          "renewsOn": null,
          "status": "known"
        }
      ]
    },
    {
      "id": "government",
      "title": "Tax and government",
      "description": "Letters, deadlines and payments.",
      "items": [
        {
          "id": "rel-at",
          "name": "Autoridade Tributária",
          "company": "Hazel Tree",
          "detail": "VAT and withholding for all three companies",
          "foundIn": "Tax letters and payments",
          "renewsOn": null,
          "status": "known"
        },
        {
          "id": "rel-ss",
          "name": "Segurança Social",
          "company": "Company B",
          "detail": "Monthly contributions",
          "foundIn": "Payment references in email",
          "renewsOn": null,
          "status": "known"
        }
      ]
    }
  ],
  "companies": [
    {
      "id": "hazel-tree",
      "name": "Hazel Tree"
    },
    {
      "id": "company-b",
      "name": "Company B"
    },
    {
      "id": "company-c",
      "name": "Company C"
    }
  ]
};
