"""How Portuguese bank statements word payments (§21, §49): the Portugal pack's :class:`BankWording`.

Consecutive folded words (upper case, no accents), as the core's expected-evidence engine
(:mod:`backoffice.reconciliation.expected`) matches them on the bank lines of Portuguese companies.
Bank-statement conventions compiled from public naming of the institutions, not legal facts; NOT verified
against live bank feeds (verified_as_of: never). Extend them as feeds are seen.
"""

from __future__ import annotations

from backoffice.countries.base import BankFeePolicy, BankWording

__all__ = ["BANK_FEE_POLICY", "BANK_WORDING"]

BANK_WORDING = BankWording(
    # The tax office and Social Security named outright ("PAG ESTADO IVA 2026/08", "SEG SOCIAL CONTRIBUICOES").
    tax_authorities=(
        "AUTORIDADE TRIBUTARIA",
        "AUTORIDADE TRIBUTARIA E ADUANEIRA",
        "PAGAMENTO AO ESTADO",
        "PAGAMENTOS AO ESTADO",
        "PAG AO ESTADO",
        "PAG ESTADO",
        "SEGURANCA SOCIAL",
        "SEG SOCIAL",
        "INSTITUTO DE GESTAO FINANCEIRA DA SEGURANCA SOCIAL",
        "IGFSS",
    ),
    # A counterparty that is only "AT": the Autoridade Tributária.
    authority_names=frozenset({"AT"}),
    # Tax abbreviations only count next to a word naming the state as payee ('PAG ESTADO IVA'), because on
    # their own they collide with ordinary words.
    tax_words=frozenset({"IVA", "IRC", "IRS", "IMI", "IUC", "IMT"}),
    state_words=frozenset({"ESTADO", "AT", "IMPOSTO", "IMPOSTOS", "FINANCAS", "TRIBUTARIA"}),
    # The municipal tourist tax paid to the municipality (a local fee, checklist X26).
    tourist_tax=("TAXA TURISTICA", "TAXAS TURISTICAS", "TAXA MUNICIPAL TURISTICA", "TAX MUN TURISTICA"),
    # Grants and subsidies paid to the business (checklist X30): the agencies and the words that mean a grant.
    grants=("IFAP", "PEPAC", "PORTUGAL 2030", "PT2030", "COMPETE 2030", "RECUPERAR PORTUGAL", "IAPMEI",
            "FUNDO AMBIENTAL", "SUBSIDIO", "SUBSIDIOS", "SUBVENCAO"),
    # Payroll allowances ("subsídio de férias"): never a grant.
    payroll_allowances=("SUBSIDIO DE FERIAS", "SUBSIDIO DE NATAL", "SUBSIDIO DE REFEICAO", "SUBSIDIO DE ALIMENTACAO"),
    bank_fees=(
        "COMISSAO",
        "COMISSOES",
        "COM MANUTENCAO",
        "MANUTENCAO DE CONTA",
        "MANUTENCAO CONTA",
        "DESPESAS DE MANUTENCAO",
        "IMPOSTO DO SELO",  # stamp duty the bank charges on its own fees and interest
        "IMPOSTO SELO",
        "IMP SELO",
        "JUROS",
    ),
    payroll_words=frozenset({"SALARIO", "SALARIOS", "VENCIMENTO", "VENCIMENTOS", "ORDENADO", "ORDENADOS",
                             "REMUNERACAO", "REMUNERACOES"}),
    loans=("PRESTACAO EMPRESTIMO", "PREST EMPRESTIMO", "AMORTIZACAO EMPRESTIMO", "EMPRESTIMO", "MUTUO"),
)


# Which of its bank's own charges the bank statement alone covers in Portugal (QA J6): the bank's fees and
# commissions, the stamp duty (imposto do selo) it charges on them and on interest, and the interest itself. A
# Portuguese bank's statement shows each of these as its own line (bank_fees above find them). An owner's or
# accountant's rule still wins.
BANK_FEE_POLICY = BankFeePolicy(
    country="Portugal",
    covers=frozenset({"fee", "stamp_duty", "interest"}),
    interest_words=("JUROS", "INTEREST"),
    stamp_duty_words=("IMPOSTO DO SELO", "IMPOSTO SELO", "IMP SELO"),
    source="Bank fees and stamp duty: the statement is enough (product policy for Portugal, QA J6).",
)
