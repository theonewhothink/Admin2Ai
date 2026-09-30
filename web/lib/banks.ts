/**
 * Banks offered in onboarding (production), as Open Banking institution ids
 * for POST /api/connections/bank/start (GoCardless Bank Account Data).
 *
 * The default list is the Portugal pack's main business banks (spec §49-50).
 * Institution ids follow GoCardless's NAME_BIC pattern; check them against
 * GET https://bankaccountdata.gocardless.com/api/v2/institutions/?country=pt
 * for your account, and override the list without a code change with
 * NEXT_PUBLIC_BANKS="Name=INSTITUTION_ID;Other bank=OTHER_ID" at build time.
 */

export interface Bank {
  name: string;
  institutionId: string;
}

export const DEFAULT_BANKS: Bank[] = [
  { name: "Millennium bcp", institutionId: "MILLENNIUMBCP_BCOMPTPL" },
  { name: "Caixa Geral de Depósitos", institutionId: "CGD_CGDIPTPL" },
  { name: "Santander", institutionId: "SANTANDER_TOTTA_TOTAPTPL" },
  { name: "BPI", institutionId: "BANCOBPI_BBPIPTPL" },
  { name: "Novo Banco", institutionId: "NOVOBANCO_BESCPTPL" },
  { name: "Revolut Business", institutionId: "REVOLUT_REVOLT21" },
];

/** "Name=ID;Name=ID" → banks. Anything malformed is skipped; nothing usable → the defaults. */
export function banksFrom(value: string | undefined): Bank[] {
  const parsed = (value ?? "")
    .split(";")
    .map((pair) => {
      const at = pair.lastIndexOf("=");
      return at > 0 ? { name: pair.slice(0, at).trim(), institutionId: pair.slice(at + 1).trim() } : null;
    })
    .filter((b): b is Bank => b !== null && b.name !== "" && /^[A-Z0-9_]+$/.test(b.institutionId));
  return parsed.length > 0 ? parsed : DEFAULT_BANKS;
}

export const banks: Bank[] = banksFrom(process.env.NEXT_PUBLIC_BANKS);
