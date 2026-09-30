"""Where the money went: every payment as the owner thinks of it (§20–21, §39, §54).

The chat answers "what did we spend in August?", "how much did Company C spend
on software?", "income last month" or "how much VAT did we pay?" from the
engine's own records, never from a model's memory. :class:`Ledger` turns every
bank payment the engine knows into a :class:`Line`: the tracked payments and
the 90-day bank history imported at onboarding (§6). Each line says who was
paid, which company it belongs to, what kind of money it is and a cost
category.

**Kind** comes from the expected-evidence engine (§21), the same decision that
drives chasing: a supplier purchase is a cost; a tax payment, a bank charge, a
salary or a loan instalment is named as such; money moved between the owner's
own accounts or companies is *not* spending, and neither is paying off a card
(its purchases are counted one by one).

**Category**, first hit wins:

1. a category rule the accountant or the owner taught (§28, "Treat all Adobe
   subscriptions as Software"): authoritative;
2. the expected-evidence decision: taxes, bank fees, salaries, loan repayments;
3. what the supplier's own documents and the bank line say ("Renda",
   "Eletricidade", "Adobe Software Portugal", "Viagem") and well-known merchant
   names. These wording tables are conventions, not facts (AMBER, §57): they
   group answers for the owner; they never close, match or file anything.

**Spending** is money out that is a real cost: supplier purchases, taxes, bank
fees, salaries and loan instalments. Answers name taxes and bank fees
separately and say what was left out. Totals are in euros.

**Other currencies** (checklist I11): a card payment in dollars or pounds that the bank charged in euros
(its line says "USD 125,00 TAXA 0,9215") is counted at the euros actually charged, never at a guessed
rate, and answers say which payments were converted (``Line.original_amount``). A payment booked in
another currency (a US dollar account) has no euro amount: it is never in a euro total; answers list it
separately, in its own currency, money in included (a customer paying in dollars into a dollar account).

**Grants and subsidies** (checklist X30): money in from IFAP, PEPAC, Portugal 2030 ... is kind ``grant``:
money in, named apart, never a sale. The municipal tourist tax (X26) is a tax paid to the municipality.

**Disputed card payments** (checklist I7): money a card network takes back when a customer disputes a
card payment (kind ``chargeback``), and money won back (kind ``chargeback_won``), are neither a cost nor
income: left out of both, and said.

**Security deposits** (checklist X9): a refundable security deposit a customer paid is theirs, held for
them (kind ``deposit_held``): never income, left out and said. What goes back is left out like any deposit
given back; a part kept becomes income only once an invoice for it or the owner's confirmation says so.

**Recharged costs and client money** (backoffice.recharges): the part of a payment bought for a client
to pay back (a reimbursable cost, a disbursement, media bought for them, a pass-through licence) is not
the business's own cost: it is its own line, kind ``recharge``, left out of spending and reported
separately with what the client already paid back. Money a client pays back for such costs (kind
``paid_back``), or pays in as client money (kind ``client_money``: a travel agency's trip money), is not
revenue either.

**Deposits** (checklist X8): money a customer paid ahead of the work (a deposit, retainer or advance)
is money in, kind ``deposit``, named apart as not invoiced yet; once the final invoice takes it off it is
simply part of that invoice's income (kind ``income``), so the invoice is never counted twice: the
deposit and the balance add up to it. A deposit given back when a booking was cancelled is neither
income nor spending: the deposit (kind ``deposit_returned``) and the money that gave it back (kind
``deposit_refund``) are left out of both, and said.

**Staff expense claims** (backoffice.staff): a receipt an employee paid with their own money is the
business's cost once the owner approved paying it back (kind ``cost``, dated on the receipt), counted once;
the transfer that later pays the employee back is not a second cost (kind ``reimbursement``, left out and
said so). Answers name what is still to be paid back, and claims waiting for the owner's OK are not
counted yet.

**Payouts** from card terminals and payment / sales platforms (SIBS, Stripe,
PayPal, Booking.com, Glovo, ...) are net settlements, never customer revenue
on their own. Once the provider's payout report has matched the bank payout
to the cent (``backoffice.settlements``), the payout is shown as what it is:
its gross sales (money in, kind ``sales``, carrying the fees, refunds,
disputed payments and adjustments behind it) and the provider's fees or
commission (a cost, kind ``platform_fee``, evidenced by the report and the
provider's commission invoice when it matched). A payout whose report has not
arrived (or disagrees with the bank) is not counted at all: answers list it as
waiting for its report.

**Cash** (backoffice.cashbook): the till's cash sales are money in from its till reports (kind ``till_cash``,
dated on the day of the report); the card part of a till report is counted through the card terminal's payouts
above, never twice. Cash paid into the bank (kind ``cash_deposit``) is that same cash going to the bank: never
counted as sales again, and said so (or, while no till report explains it, said as not counted yet). Cash taken
out of the bank for the cash box (kind ``cash_withdrawal``) is not a cost: the cash receipts it paid for are.

**Direct debits that came back** (backoffice.members): a member's payment and the bank line that gave it back
(kinds ``payment_returned`` and ``debit_returned``) are neither income nor a cost, and are said so; the period
they were for is unpaid again until the member's next payment, which is income when it arrives.

Pure Python (runs in the browser build too).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from backoffice.domain.models import DocumentType, Transaction, TransactionKind
from backoffice.learning import RuleSubject, counterparty_key, day_month, display_name, fold, format_money
from backoffice.reconciliation import (
    EvidenceExpectation,
    ExpectedEvidenceEngine,
    fx_from_text,
    payout_provider,
    provider_named,
)

if TYPE_CHECKING:  # pragma: no cover
    from backoffice.service import BackOfficeService

__all__ = ["CATEGORIES", "Category", "Ledger", "Line", "Money", "VatResult", "category_label"]

CURRENCY = "EUR"
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December")


# --------------------------------------------------------------------------- categories


@dataclass(frozen=True)
class Category:
    """A cost category: how the owner asks for it and what evidence puts a payment in it."""

    id: str
    label: str
    asked: tuple[str, ...]  # folded words an owner uses ("phone", "internet")
    evidence: tuple[str, ...] = ()  # folded words on invoices, bank lines or merchant names


# Engine kinds first (decided by the expected-evidence engine), then evidence wording.
# The wording tables are conventions (unverified against live feeds): extend them as data is seen.
CATEGORIES: tuple[Category, ...] = (
    Category("tax", "Taxes", ("tax", "taxes", "taxation", "tax office", "tax authority", "tax payments",
                              "impostos", "imposto", "financas", "social security", "seguranca social",
                              "irs", "irc", "withholding", "retencoes")),
    Category("bank_fees", "Bank fees", ("bank fees", "bank fee", "bank charges", "bank charge", "bank costs",
                                        "fees", "commissions", "comissoes", "account fees", "maintenance fees")),
    Category("payroll", "Salaries", ("salaries", "salary", "payroll", "wages", "staff costs", "ordenados",
                                     "salarios", "vencimentos")),
    Category("loan", "Loan repayments", ("loan", "loans", "loan repayments", "repayments", "emprestimo",
                                         "emprestimos")),
    Category("rent", "Rent", ("rent", "rents", "rental", "lease", "landlord", "renda", "rendas", "arrendamento",
                              "aluguer", "office rent"),
             ("renda", "rendas", "rent", "arrendamento", "aluguer", "senhorio", "landlord", "lease")),
    Category("telecom", "Phone and internet", ("telecom", "telecoms", "telco", "phone", "phones", "telephone",
                                               "mobile", "internet", "broadband", "telemovel", "telecomunicacoes",
                                               "comunicacoes", "fibre", "fiber"),
             ("comunicacoes", "telecomunicacoes", "telecom", "internet", "telemovel", "servicos moveis", "fibra",
              "broadband", "mobile phone", "vodafone", "meo", "nos comunicacoes", "nowo", "digi mobil")),
    Category("energy", "Electricity, gas and water", ("electricity", "energy", "power", "utilities", "utility",
                                                      "gas", "water", "eletricidade", "electricidade", "energia",
                                                      "luz", "agua"),
             ("eletricidade", "electricidade", "electricity", "energia", "energy", "gas natural", "agua",
              "water", "edp", "galp", "endesa", "iberdrola", "goldenergy", "epal")),
    Category("software", "Software", ("software", "saas", "licences", "licenses", "licence", "license", "apps",
                                      "cloud"),
             ("software", "saas", "licenca", "licence", "license", "creative cloud", "adobe", "microsoft",
              "google workspace", "notion", "slack", "dropbox", "atlassian", "github", "canva", "zoom",
              "openai", "anthropic")),
    Category("travel", "Travel", ("travel", "trips", "taxi", "taxis", "rides", "transport", "transportation",
                                  "flights", "flight", "train", "trains", "fuel", "petrol", "parking", "tolls",
                                  "viagens", "deslocacoes", "combustivel", "portagens"),
             ("viagem", "viagens", "trip", "taxi", "uber", "bolt", "free now", "ryanair", "easyjet", "tap air",
              "comboios", "via verde", "portagens", "combustivel", "gasolina", "fuel", "parking",
              "estacionamento", "airbnb", "booking com", "hotel")),
    Category("meals", "Meals", ("meals", "meal", "food", "restaurants", "restaurant", "lunch", "lunches",
                                "dinner", "dinners", "coffee", "eating out", "refeicoes", "restaurantes",
                                "almocos", "jantares"),
             ("restaurante", "restaurant", "pastelaria", "snack bar", "refeicao", "refeicoes", "glovo",
              "uber eats", "bolt food", "cervejaria", "tasca")),
    Category("office", "Office and furniture", ("furniture", "office supplies", "office equipment", "supplies",
                                                "equipment", "stationery", "moveis", "mobiliario",
                                                "material de escritorio", "office"),
             ("moveis e decoracao", "mobiliario", "furniture", "estante", "secretaria", "cadeira",
              "material de escritorio", "papelaria", "office supplies", "ikea", "staples", "worten")),
    Category("insurance", "Insurance", ("insurance", "insurances", "seguro", "seguros", "premiums"),
             ("seguro", "seguros", "insurance", "apolice", "fidelidade", "allianz", "ageas", "tranquilidade",
              "generali", "zurich", "mapfre")),
    # What card terminals and sales platforms keep from payouts: only from a matched payout report,
    # never from wording (no evidence words).
    Category("platform_fees", "Payment and platform fees", ("platform fees", "payment fees", "card fees",
                                                           "processing fees", "payout fees", "platform commissions",
                                                           "sales commissions")),
)
OTHER = Category("other", "Other costs", ())
_BY_ID = {c.id: c for c in (*CATEGORIES, OTHER)}
_EVIDENCE_PHRASES = sorted(((p, c.id) for c in CATEGORIES for p in c.evidence), key=lambda x: -len(x[0]))

# Expected evidence (§21) -> what kind of money a payment is.
_KIND: dict[EvidenceExpectation, str] = {
    EvidenceExpectation.INVOICE: "cost",
    EvidenceExpectation.RECEIPT: "cost",
    EvidenceExpectation.TAX_NOTICE_OR_PROOF: "tax",
    EvidenceExpectation.BANK_EVIDENCE_SUFFICES: "bank_fee",
    EvidenceExpectation.PAYROLL: "payroll",
    EvidenceExpectation.LOAN_STATEMENT: "loan",
    EvidenceExpectation.CARD_STATEMENT: "card_repayment",
    EvidenceExpectation.NONE_INTERNAL_TRANSFER: "transfer",
    EvidenceExpectation.SALES_INVOICE: "income",
    EvidenceExpectation.REFUND_OR_CREDIT_NOTE: "refund",
    EvidenceExpectation.PAYOUT_REPORT: "payout",  # a net settlement: shown through its report, never as income
}
_KIND_CATEGORY = {"tax": "tax", "bank_fee": "bank_fees", "payroll": "payroll", "loan": "loan",
                  "platform_fee": "platform_fees"}
COST_KINDS = frozenset({"cost", "tax", "bank_fee", "payroll", "loan", "platform_fee"})
IN_KINDS = frozenset({"income", "refund", "interest", "tax_refund", "sales", "deposit", "grant", "till_cash"})
NOT_SPENDING = frozenset({"transfer", "card_repayment", "reimbursement", "cash_withdrawal"})
RETURNED = frozenset({"payment_returned", "debit_returned"})  # a direct debit that came back, and its payment
GIVEN_BACK = frozenset({"deposit_returned", "deposit_refund"})  # a deposit and the money that gave it back
DISPUTED = frozenset({"chargeback", "chargeback_won"})  # a disputed card payment taken back, or won back (I7)
HELD_FOR = frozenset({"deposit_held"})  # a security deposit held for a customer (X9)
_ZERO = Decimal(0)
_PURCHASE_DOCS = frozenset({DocumentType.INVOICE, DocumentType.INVOICE_RECEIPT, DocumentType.SIMPLIFIED_INVOICE,
                            DocumentType.RECEIPT, DocumentType.DEBIT_NOTE, DocumentType.CREDIT_NOTE})
_TEXT_MIME = re.compile(r"^(?:text/|application/(?:xml|json)|message/)")


def category_label(category_id: str) -> str:
    c = _BY_ID.get(category_id)
    if c is not None:
        return c.label
    return category_id.split(":", 1)[-1]


def _has_phrase(folded: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", folded) is not None


# --------------------------------------------------------------------------- lines


@dataclass(frozen=True)
class Line:
    """One payment in or out, as the owner thinks of it."""

    id: str
    on: date
    amount: Decimal  # always positive; see ``direction``
    direction: str  # "out" | "in"
    currency: str
    merchant: str
    supplier_id: str | None
    company_id: str | None  # None while the owner has not said (``pending``) or when personal
    pending: bool
    private: bool
    # cost | tax | bank_fee | payroll | loan | transfer | card_repayment | income | refund | interest |
    # payout (a provider's net settlement, never counted itself) | sales | platform_fee (from its payout report)
    kind: str
    category: str
    evidence_id: str | None  # stored evidence of the bank line (tracked payments only), or of the payout report
    document_ids: tuple[str, ...]
    history: bool  # from the bank history imported at onboarding (§6), not tracked item by item
    needs_document: bool
    has_document: bool
    description: str = ""
    paid_in_cash: bool = False  # a receipt that says it was paid in cash: no bank line (§11)
    # Sales from a payout report (kind "sales"): what the provider kept and what reached the bank.
    fees: Decimal = _ZERO
    refunds: Decimal = _ZERO
    chargebacks: Decimal = _ZERO
    adjustments: Decimal = _ZERO
    paid_out: Decimal = _ZERO
    provider: str = ""  # the payout provider in a sentence ("Stripe", "your card terminal")
    fee_word: str = ""  # what that provider calls what it keeps: "fees" or "commission"
    status: str = ""  # payouts: "settled" | "report_missing" | "report_disagrees" | "report_does_not_add_up"
    client: str = ""  # recharge / paid_back / client_money: the client's cost center
    recovered: Decimal = _ZERO  # recharge: the part the client has already paid back
    # Staff expense claims (backoffice.staff): who paid it with their own money, and the claim's status
    # ("waiting" | "approved" | "paid"); a reimbursement: who was paid back.
    employee: str = ""
    claim_status: str = ""
    # A payment in another currency the bank charged (or paid in) in euros: what it was in that currency (I11).
    original_amount: Decimal | None = None
    original_currency: str = ""

    @property
    def category_label(self) -> str:
        return category_label(self.category)

    @property
    def converted(self) -> bool:
        return self.original_amount is not None and bool(self.original_currency)


@dataclass
class Money:
    """A total over some lines, with what was counted, left out and why (card-ready)."""

    start: date
    end: date
    direction: str
    lines: list[Line]
    total: Decimal
    group_by: str
    rows: list[tuple[str, Decimal, int]]
    by_kind: dict[str, Decimal]
    left_out: list[Line]  # transfers between own accounts/companies, card repayments
    pending: list[Line]  # not counted: waiting for the owner to say which company
    private_count: int
    other_currency: int
    covered: tuple[date, date] | None  # the part of the period the records cover; None: no records at all
    history_months: list[str]
    previous: Money | None = None
    previous_label: str = ""
    # Money in only: payouts not counted because their payout report has not proven them yet.
    waiting_payouts: list[Line] = field(default_factory=list)
    # Not the business's own: costs bought for clients to pay back (out), and what clients paid back or
    # paid in as client money (in).
    recharged: list[Line] = field(default_factory=list)
    paid_back: list[Line] = field(default_factory=list)
    # Deposits given back when a booking was cancelled (in), or the money that gave them back (out): never counted.
    given_back: list[Line] = field(default_factory=list)
    # Staff expense claims waiting for the owner's OK (not counted yet).
    claims_waiting: list[Line] = field(default_factory=list)
    # Disputed card payments taken back or won back (I7), and security deposits held for customers (X9): never
    # counted, always said.
    disputed: list[Line] = field(default_factory=list)
    held_for: list[Line] = field(default_factory=list)
    # Payments with no euro amount (booked in another currency): never in a euro total, listed apart (I11).
    other_currency_lines: list[Line] = field(default_factory=list)
    # Cash paid into the bank (in): the till's cash, counted from its till reports, never twice.
    cash_banked: list[Line] = field(default_factory=list)
    # Direct debits that came back, and the payments they returned: neither income nor a cost.
    returned: list[Line] = field(default_factory=list)

    @property
    def converted(self) -> list[Line]:
        """Counted payments in another currency, at the euros the bank actually charged (or paid in)."""
        return [x for x in self.lines if x.converted]

    @property
    def count(self) -> int:
        return len(self.lines)

    @property
    def to_pay_back(self) -> list[Line]:
        """Counted expense claims the employee has not been paid back for yet."""
        return [x for x in self.lines if x.claim_status == "approved"]

    @property
    def reimbursements(self) -> list[Line]:
        """Transfers paying employees back: left out, their receipts are the cost."""
        return [x for x in self.left_out if x.kind == "reimbursement"]

    @property
    def sales(self) -> list[Line]:
        """Gross sales proven by payout reports (card terminals, payment and sales platforms)."""
        return [x for x in self.lines if x.kind == "sales"]


@dataclass
class VatResult:
    start: date
    end: date
    purchases: list[dict[str, Any]]
    purchase_vat: Decimal
    sales: list[dict[str, Any]]
    sales_vat: Decimal
    paid_to_state: list[Line]
    pending: list[dict[str, Any]]


# --------------------------------------------------------------------------- the ledger


class Ledger:
    """Every payment the engine knows, classified once per question."""

    def __init__(self, service: BackOfficeService) -> None:
        self.svc = service
        self.repo = service.repo
        self.today = service._today()
        self._supplier_text: dict[str, str] = {}
        # Payout reports by the bank payout they were paired with (settled, or disagreeing with it).
        self._payout_reports: dict[str, Any] = {}
        self.recharges: Any = None  # backoffice.recharges.RechargeBook, when any cost is on a client
        for s in sorted(getattr(self.repo, "settlements", {}).values(), key=lambda s: s.document_id):
            if s.transaction_id and (s.settled or s.transaction_id not in self._payout_reports):
                self._payout_reports[s.transaction_id] = s
        self.lines = self._build()

    # -- building -------------------------------------------------------------

    def _build(self) -> list[Line]:
        repo = self.repo
        resolver = repo.resolver()
        engine = ExpectedEvidenceEngine(entities=repo.entities, suppliers=resolver)
        pending = {n.subject_id for n in repo.open_needs() if n.kind == "choice" and n.subject_type == "transaction"}
        out: list[Line] = []
        for tx in repo.history_transactions:
            out.append(self._line(tx, None, engine.classify(tx), resolver, pending))
        for rec in repo.transactions.values():
            decision = rec.decision or engine.classify(rec.tx)
            out.append(self._line(rec.tx, rec, decision, resolver, pending))
        for record in repo.documents.values():
            cash = self._cash_line(record)
            if cash is not None:
                out.append(cash)
        for claim in getattr(repo, "expense_claims", {}).values():
            line = self._claim_line(claim)
            if line is not None:
                out.append(line)
        for till in getattr(repo, "till_days", {}).values():
            line = self._till_line(till)
            if line is not None:
                out.append(line)
        for receipt in getattr(repo, "member_receipts", {}).values():
            line = self._desk_line(receipt)
            if line is not None:
                out.append(line)
        out = self._client_parts(out)
        out = self._deposit_parts(out)
        out += self._settled_payouts({x.id: x for x in out})
        out.sort(key=lambda x: (x.on, x.id))
        return out

    def _client_parts(self, lines: list[Line]) -> list[Line]:
        """The parts of payments that are a client's, not the business's own (module docstring)."""
        from backoffice.recharges import RechargeBook

        if not any(getattr(r.tx.cost_allocation, "shares", None) for r in self.repo.transactions.values()):
            return lines
        book = self.recharges = RechargeBook(self.repo)
        out: list[Line] = []
        for x in lines:
            parts = book.out_parts.get(x.id) if x.direction == "out" else book.in_parts.get(x.id)
            if x.history or not parts:
                out.append(x)
                continue
            client_money = x.direction == "in" and all(book.clients[cid].client_money for cid, _ in parts)
            kind = "recharge" if x.direction == "out" else "client_money" if client_money else "paid_back"
            taken = sum((part for _, part in parts), _ZERO)
            for i, (cid, part) in enumerate(parts):
                recovered = book.clients[cid].recovered.get(x.id, _ZERO) if kind == "recharge" else _ZERO
                whole = taken >= x.amount and i == 0
                # The whole payment keeps its own id (so a missing invoice is still found); a part gets its own.
                out.append(replace(x, id=x.id if whole else f"{x.id}:{kind}:{cid}", amount=part, kind=kind,
                                   client=cid, recovered=recovered,
                                   needs_document=x.needs_document if whole else False))
            if taken < x.amount:
                out.append(replace(x, amount=x.amount - taken))
        return out

    def _deposit_parts(self, lines: list[Line]) -> list[Line]:
        """Deposits as the owner thinks of them (module docstring): held apart until invoiced, part of the
        invoice once it takes them off, left out when they went back (a part kept stays counted)."""
        repo = self.repo
        deposits = getattr(repo, "deposits", {})
        if not deposits:
            return lines
        out: list[Line] = []
        for x in lines:
            rec = repo.transactions.get(x.id)
            if x.history or rec is None:
                out.append(x)
                continue
            if rec.deposit_refund_of and rec.deposit_refund_of in deposits:
                out.append(replace(x, kind="deposit_refund", category="deposit_refund", needs_document=False))
                continue
            dep = deposits.get(x.id)
            if dep is None or dep.direction != "in" or x.direction != "in":
                out.append(x)
                continue
            if getattr(dep, "security", False):
                out += self._security_parts(x, dep)
                continue
            waiting = dep.status == "held" and dep.advance_document_id is None  # not on any invoice yet
            kept = replace(x, kind="deposit" if waiting else "income", category="deposit" if waiting else x.category)
            if dep.returned <= 0:
                out.append(kept)
                continue
            out.append(replace(x, id=f"{x.id}:given_back" if dep.available > 0 else x.id, amount=dep.returned,
                               kind="deposit_returned", category="deposit_returned", needs_document=False))
            if dep.available > 0:
                out.append(replace(kept, amount=dep.available))
        return out

    @staticmethod
    def _security_parts(x: Line, dep: Any) -> list[Line]:
        """A security deposit (X9): what is still held for the customer (never income), what went back (neither
        income nor a cost), and what was kept, as income, once an invoice or the owner's OK says so."""
        parts: list[Line] = []
        if dep.returned > 0:
            parts.append(replace(x, id=f"{x.id}:given_back", amount=dep.returned, kind="deposit_returned",
                                 category="deposit_returned", needs_document=False))
        rest = dep.amount - dep.returned
        if rest > 0:
            kept = dep.status in ("applied", "kept")
            parts.append(replace(x, id=f"{x.id}:kept" if kept else f"{x.id}:held", amount=rest,
                                 kind="income" if kept else "deposit_held",
                                 category="income" if kept else "deposit_held", needs_document=False,
                                 description="Kept from a security deposit" if kept else "Security deposit held"))
        if parts:  # the whole payment keeps its own id on one of its parts (so it is still found)
            parts[-1] = replace(parts[-1], id=x.id)
        return parts

    def _cash_line(self, record: Any) -> Line | None:
        """A purchase paid in cash: its receipt is its only evidence (§11). Counted once the receipt
        closed as a company's cost; waiting for the owner's answer it is pending, like any payment."""
        repo = self.repo
        if not record.paid_in_cash or record.matched_tx_ids or record.document.gross_amount is None \
                or getattr(record, "claim_id", None):  # an employee's own money: its expense claim's line
            return None
        from backoffice.domain.lifecycle import Stage

        stage = repo.items[record.item_id].stage
        doc = record.document
        if stage is Stage.CLOSED and doc.entity_id:
            company, waiting = doc.entity_id, False
        elif stage is Stage.NEEDS_OWNER:
            company, waiting = None, True
        else:
            return None  # set aside by the owner, or still being read
        text = self._document_text(record)
        category = next((cat for phrase, cat in _EVIDENCE_PHRASES if _has_phrase(text, phrase)), OTHER.id)
        return Line(
            id=record.id, on=doc.issue_date or record.received_at.date(), amount=abs(doc.gross_amount),
            direction="out", currency=doc.currency, merchant=display_name(doc.supplier_name),
            supplier_id=record.supplier_id, company_id=company, pending=waiting, private=False, kind="cost",
            category=category, evidence_id=record.evidence_ids[0] if record.evidence_ids else None,
            document_ids=(record.id,), history=False, needs_document=True, has_document=True,
            description="Paid in cash", paid_in_cash=True,
        )

    def _till_line(self, till: Any) -> Line | None:
        """The cash sales of one day's till report (the card part is counted through the card payouts)."""
        if not till.usable or not till.till.cash:
            return None
        record = self.repo.documents.get(till.document_id)
        day = till.till
        return Line(
            id=f"{till.document_id}:cash", on=day.day, amount=day.cash, direction="in", currency=day.currency,
            merchant="Cash sales", supplier_id=None, company_id=till.company_id, pending=False, private=False,
            kind="till_cash", category="sales",
            evidence_id=record.evidence_ids[0] if record is not None and record.evidence_ids else None,
            document_ids=(till.document_id,), history=False, needs_document=False, has_document=True,
            description=f"Till report{' ' + day.number if day.number else ''}", paid_in_cash=True,
        )

    def _desk_line(self, receipt: Any) -> Line | None:
        """A member who paid in cash at the desk: the business's receipt is the only record (no bank line)."""
        if not receipt.row.cash or receipt.status != "paid" or receipt.company_id is None:
            return None
        record = self.repo.documents.get(receipt.document_id)
        row = receipt.row
        return Line(
            id=receipt.document_id, on=row.issued_on, amount=row.amount, direction="in", currency="EUR",
            merchant=row.member, supplier_id=None, company_id=receipt.company_id, pending=False, private=False,
            kind="income", category="income",
            evidence_id=record.evidence_ids[0] if record is not None and record.evidence_ids else None,
            document_ids=(receipt.document_id,), history=False, needs_document=False, has_document=True,
            description=f"Receipt {row.number}, {row.period_label}", paid_in_cash=True,
        )

    def _claim_line(self, claim: Any) -> Line | None:
        """A receipt an employee paid with their own money (backoffice.staff): the business's cost once
        approved, counted once, on the receipt's date; waiting for the owner's OK it is not counted yet."""
        if claim.status not in ("waiting", "approved", "paid"):
            return None  # declined: not a company cost
        repo = self.repo
        record = repo.documents.get(claim.document_id)
        employee = repo.employees.get(claim.employee_id)
        who = employee.name if employee is not None else "an employee"
        text = self._document_text(record) if record is not None else ""
        category = next((cat for phrase, cat in _EVIDENCE_PHRASES if _has_phrase(text, phrase)), OTHER.id)
        description = {"waiting": f"Paid by {who}, waiting for your OK to pay it back",
                       "approved": f"Paid by {who}, to pay back", "paid": f"Paid by {who}, paid back"}[claim.status]
        return Line(
            id=claim.id, on=claim.spent_on, amount=claim.amount, direction="out", currency=claim.currency,
            merchant=claim.merchant, supplier_id=record.supplier_id if record is not None else None,
            company_id=claim.company_id, pending=False, private=False,
            kind="cost" if claim.status != "waiting" else "claim_waiting", category=category,
            evidence_id=claim.evidence_ids[0] if claim.evidence_ids else None, document_ids=(claim.document_id,),
            history=False, needs_document=True, has_document=True, description=description, employee=who,
            claim_status=claim.status,
        )

    def _settled_payouts(self, by_id: dict[str, Line]) -> list[Line]:
        """A payout its report proved, as what it is: gross sales in, the provider's fees out."""
        repo = self.repo
        out: list[Line] = []
        for tx_id, s in sorted(self._payout_reports.items()):
            payout = by_id.get(tx_id)
            if payout is None or not s.settled:
                continue
            report = s.report
            doc = repo.documents.get(s.document_id)
            evidence = doc.evidence_ids[0] if doc is not None and doc.evidence_ids else payout.evidence_id
            common: dict[str, Any] = {
                "on": payout.on, "currency": report.currency, "supplier_id": None, "company_id": payout.company_id,
                "pending": payout.pending, "private": payout.private, "evidence_id": evidence, "history": False,
                "needs_document": False, "has_document": True, "provider": payout.provider,
            }
            ref = f" {report.payout_id}" if report.payout_id else ""
            if report.gross_sales > 0:
                out.append(Line(
                    id=f"{tx_id}:sales", amount=report.gross_sales, direction="in",
                    merchant=f"Sales through {payout.provider}", kind="sales", category="sales",
                    document_ids=(s.document_id,), description=f"Payout{ref}", fees=report.fees,
                    refunds=report.refunds, chargebacks=report.chargebacks, adjustments=report.adjustments,
                    paid_out=report.net, fee_word=report.provider.fee_word, **common))
            if report.fees > 0:
                out.append(Line(
                    id=f"{tx_id}:fees", amount=report.fees, direction="out", merchant=payout.merchant,
                    kind="platform_fee", category="platform_fees",
                    document_ids=(s.document_id, *s.commission_document_ids),
                    description=f"{report.provider.fee_word.capitalize()} kept from the payout{ref}", **common))
        return out

    def _line(self, tx: Transaction, rec: Any, decision: Any, resolver: Any, pending: set[str]) -> Line:
        repo = self.repo
        match = resolver.resolve_transaction(tx)
        supplier = match.supplier
        folded = fold(f"{tx.counterparty} {tx.description}")
        kind = _KIND.get(decision.expectation, "cost")
        if kind == "bank_fee" and not (tx.kind is TransactionKind.FEE or decision.rule.startswith("bank_fee")):
            kind = "cost"  # "the bank statement is enough" taught for something that is not a bank charge
        if tx.amount > 0:
            kind = {"bank_fee": "interest", "tax": "tax_refund", "cost": "income"}.get(kind, kind)
        if decision.rule == "grant" and tx.amount > 0:
            kind = "grant"  # a grant or subsidy: money in, never a sale (X30)
        elif decision.rule in DISPUTED:
            kind = decision.rule  # a disputed card payment: neither a cost nor income (I7)
        claims = list(getattr(rec, "claim_ids", None) or []) if rec is not None else []
        if claims:
            kind = "reimbursement"  # pays an employee back: the receipts they paid are the cost (counted once)
        if decision.rule == "cash_deposit" and tx.amount > 0:
            kind = "cash_deposit"  # the till's cash going to the bank: its till reports are the sales
        elif decision.rule in ("cash_withdrawal", "cash_box") and tx.amount < 0:
            kind = "cash_withdrawal"  # cash for the cash box: its receipts are the costs
        if rec is not None and rec.id in getattr(repo, "payment_returns", {}):
            kind = "debit_returned"  # gave a member's payment back
        elif rec is not None and rec.id in getattr(repo, "returned_payments", {}):
            kind = "payment_returned"  # a member's payment that came back
        own = next((e.name for e in repo.entities if tx.counterparty_iban and tx.counterparty_iban in e.own_ibans),
                   None)
        provider, status = "", ""
        if kind == "payout":
            settlement = self._payout_reports.get(tx.id)
            found = payout_provider(tx) or provider_named(tx.counterparty, tx.description)
            if settlement is not None and (found is None or found.is_card_terminal):
                found = settlement.report.provider  # the acquirer the report names beats a bare 'TPA' line
            provider = found.label if found else display_name(tx.counterparty)
            merchant = found.title if found else provider
            if settlement is None:
                status = "report_missing"
            elif settlement.settled:
                status = "settled"
            else:
                status = "report_does_not_add_up" if settlement.problem == "does_not_add_up" else "report_disagrees"
        elif kind == "tax" and decision.rule == "tourist_tax":
            merchant = "Tourist tax"  # paid to the municipality (X26)
        elif kind == "tax":
            merchant = "Social Security" if re.search(r"\bseg(?:uranca)? social\b|\bigfss\b", folded) else "Tax office"
        elif kind == "grant":
            from backoffice.closure.obligations import grant_agency

            merchant = grant_agency(f"{tx.counterparty} {tx.description}") or display_name(tx.counterparty)
        elif kind in DISPUTED:
            chargebacks = getattr(repo, "chargebacks", {})
            cb = chargebacks.get(tx.id)
            merchant = cb.provider if cb is not None and cb.provider else display_name(tx.counterparty)
        elif kind == "transfer" and own:
            merchant = own
        elif kind in ("bank_fee", "interest") and repo.accounts.get(tx.account_id) is not None:
            merchant = repo.accounts[tx.account_id].bank
        else:
            merchant = self.svc.orchestrator.merchant_name(tx)
        # Which company: what the engine decided; the owner's answers win; history uses what was learned.
        private = bool(rec is not None and rec.private)
        waiting = rec is not None and rec.id in pending and not tx.entity_id
        if private or waiting:
            company = None
        elif tx.entity_id:
            company = tx.entity_id
        elif rec is not None:
            company = rec.company_id
        else:
            company = self._history_company(tx)
            waiting = company is None
        # Taught categories: only the rules of this company's accountant, for this company (§28, §51).
        account = repo.accounts.get(tx.account_id)
        rule_company = company or (account.holder_id if account is not None else None)
        category = self._category(tx, kind, supplier, match.key, rec, folded,
                                  repo.accountant_ids_for(rule_company), rule_company)
        docs = tuple(rec.document_ids) if rec is not None else ()
        # Charged (or paid in) in euros for an amount in another currency: the bank's conversion line (I11).
        fx = fx_from_text(f"{tx.counterparty} {tx.description}", tx.currency) if tx.currency == CURRENCY else None
        employee = ""
        if claims:
            first = repo.expense_claims.get(claims[0])
            person = repo.employees.get(first.employee_id) if first is not None else None
            employee = person.name if person is not None else ""
        return Line(
            employee=employee,
            id=tx.id, on=tx.booked_on, amount=abs(tx.amount), direction="in" if tx.amount > 0 else "out",
            currency=tx.currency, merchant=merchant, supplier_id=supplier.id if supplier else None,
            company_id=company, pending=waiting, private=private, kind=kind, category=category,
            evidence_id=rec.evidence_id if rec is not None else None, document_ids=docs,
            history=rec is None,
            needs_document=bool(decision.requires_document) and (kind in COST_KINDS or kind == "payout"),
            has_document=bool(docs or (rec is not None and rec.proof_evidence_ids)),
            description=tx.description, provider=provider, status=status,
            original_amount=fx.original_amount if fx is not None else None,
            original_currency=fx.original_currency if fx is not None else "",
        )

    def _history_company(self, tx: Transaction) -> str | None:
        """History rows: the account's own company, else what the owner answered at onboarding (§6)."""
        repo = self.repo
        account = repo.accounts.get(tx.account_id)
        if account is not None and account.owned:
            return account.holder_id
        key = counterparty_key(tx.counterparty)
        answers = {company for k, company in repo.history_pairs if k == key}
        return answers.pop() if len(answers) == 1 else None

    def _category(self, tx: Transaction, kind: str, supplier: Any, key: str, rec: Any, folded: str,
                  accountant_ids: list[str], company: str | None = None) -> str:
        repo = self.repo
        if kind in ("cash_deposit", "cash_withdrawal"):
            return "cash"  # cash between the bank and the cash box (its label is the owner's word)
        if kind in RETURNED:
            return "returned"
        if kind in ("transfer", "card_repayment", "payout", "reimbursement") or kind in IN_KINDS or kind in DISPUTED:
            return kind
        try:
            taught = repo.rulebook.evaluate(RuleSubject.from_transaction(tx, key=key, entity_id=company),
                                            tenant_id=repo.tenant_id, accountant_ids=accountant_ids).category
        except Exception:  # a rule conflict never breaks an answer; the engine reports it elsewhere
            taught = None
        if taught:
            wanted = fold(taught)
            for c in CATEGORIES:
                if wanted == fold(c.label) or wanted in c.asked:
                    return c.id
            return f"custom:{taught}"
        if kind in _KIND_CATEGORY:
            return _KIND_CATEGORY[kind]
        text = folded
        if supplier is not None:
            text += " " + self._text_for_supplier(supplier)
        if rec is not None:
            for doc_id in rec.document_ids:
                doc = repo.documents.get(doc_id)
                if doc is not None:
                    text += " " + self._document_text(doc)
        for phrase, cat in _EVIDENCE_PHRASES:
            if _has_phrase(text, phrase):
                return cat
        return OTHER.id

    def _text_for_supplier(self, supplier: Any) -> str:
        if supplier.id not in self._supplier_text:
            parts = [fold(supplier.name), *(fold(a) for a in supplier.aliases)]
            for doc in self.repo.documents.values():
                if doc.supplier_id == supplier.id:
                    parts.append(self._document_text(doc))
            self._supplier_text[supplier.id] = " ".join(parts)
        return self._supplier_text[supplier.id]

    def _document_text(self, doc: Any) -> str:
        parts = [doc.document.supplier_name or "", doc.message_text[:2000]]
        for evidence_id in doc.evidence_ids[:3]:
            try:
                ev = self.repo.registry.get(self.repo.tenant_id, evidence_id)
                if ev.mime_type and _TEXT_MIME.match(ev.mime_type):
                    parts.append(self.repo.registry.open(self.repo.tenant_id, evidence_id)[:6000]
                                 .decode("utf-8", errors="ignore"))
            except Exception:  # unreadable evidence only means fewer words to go on
                continue
        return re.sub(r"[^a-z0-9]+", " ", fold(" ".join(parts)))

    # -- coverage -------------------------------------------------------------

    def coverage(self) -> tuple[date, date]:
        """First day the bank records cover, and today."""
        starts = [c.covered_from.date() for c in self.repo.connectors.values()
                  if c.kind == "bank" and c.covered_from is not None]
        if self.lines:
            starts.append(self.lines[0].on)
        return (min(starts) if starts else self.today), self.today

    def tracked_from(self) -> date | None:
        """First day of payments checked one by one (before it: the imported history)."""
        tracked = [x.on for x in self.lines if not x.history]
        return min(tracked) if tracked else None

    def _history_months(self, lines: Iterable[Line]) -> list[str]:
        months = sorted({(x.on.year, x.on.month) for x in lines if x.history})
        return [MONTH_NAMES[m - 1] if y == self.today.year else f"{MONTH_NAMES[m - 1]} {y}" for y, m in months]

    # -- queries --------------------------------------------------------------

    def select(self, *, start: date | None = None, end: date | None = None, direction: str | None = None,
               company_ids: Sequence[str] = (), supplier_ids: Sequence[str] = (), category: str | None = None,
               amount: Decimal | None = None) -> list[Line]:
        out = []
        for x in self.lines:
            if start and x.on < start or end and x.on > end:
                continue
            if direction and x.direction != direction:
                continue
            if company_ids and x.company_id not in company_ids:
                continue
            if supplier_ids and x.supplier_id not in supplier_ids:
                continue
            if category and x.category != category:
                continue
            if amount is not None and abs(x.amount - amount) > Decimal("0.005"):
                continue
            out.append(x)
        return out

    def money(self, start: date, end: date, *, direction: str = "out", company_ids: Sequence[str] = (),
              supplier_ids: Sequence[str] = (), category: str | None = None, exclude: Sequence[str] = (),
              group_by: str | None = None, compare: Any = None) -> Money:
        """Money out (real costs) or in over a period, with what was left out and why.

        ``compare`` is None, or a ``(start, end, label)`` for the period to compare with.
        """
        first, last = self.coverage()
        covered = None if end < first or start > last else (max(start, first), min(end, last))
        base = self.select(start=start, end=end, direction=direction, supplier_ids=supplier_ids)
        wanted = COST_KINDS if direction == "out" else IN_KINDS
        counted: list[Line] = []
        left_out: list[Line] = []
        pending: list[Line] = []
        waiting: list[Line] = []
        clients: list[Line] = []
        given_back: list[Line] = []
        claims_waiting: list[Line] = []
        disputed: list[Line] = []
        held_for: list[Line] = []
        foreign: list[Line] = []
        cash_banked: list[Line] = []
        returned: list[Line] = []
        private = other_currency = 0
        for x in base:
            if x.private:
                private += 1
                continue
            if x.kind in DISPUTED or x.kind in HELD_FOR:
                # A disputed card payment taken back or won back, a security deposit held: never counted.
                if not company_ids or x.company_id in company_ids:
                    (disputed if x.kind in DISPUTED else held_for).append(x)
                continue
            if x.kind in GIVEN_BACK:
                # A deposit that went back, and the money that gave it back: neither income nor a cost.
                if not company_ids or x.company_id in company_ids:
                    given_back.append(x)
                continue
            if x.kind in RETURNED:
                # A member's payment that came back, and the bank line that gave it back: neither in nor out.
                if not company_ids or x.company_id in company_ids:
                    returned.append(x)
                continue
            if x.kind == "cash_deposit":
                # Cash paid into the bank: the till's cash sales are counted from its till reports.
                if not company_ids or x.company_id in company_ids:
                    cash_banked.append(x)
                continue
            if x.kind == "claim_waiting":
                # An employee's receipt waiting for the owner's OK: not a company cost yet.
                if not company_ids or x.company_id in company_ids:
                    claims_waiting.append(x)
                continue
            if x.kind in ("recharge", "paid_back", "client_money"):
                # A client's money, not the business's own: reported on its own, never in a total.
                if (not company_ids or x.company_id in company_ids) and (not category or x.category == category
                                                                          or x.kind != "recharge"):
                    clients.append(x)
                continue
            if x.kind == "payout":
                # A net settlement: counted through its report's sales and fees, or not at all yet.
                if x.status != "settled" and (not company_ids or x.company_id in company_ids or x.pending):
                    waiting.append(x)
                continue
            if x.kind in NOT_SPENDING:
                if not company_ids or x.company_id in company_ids:
                    left_out.append(x)
                continue
            if x.kind not in wanted:
                continue
            if category and x.category != category or x.category in exclude:
                continue
            if company_ids and x.company_id not in company_ids:
                if x.pending:
                    pending.append(x)
                continue
            if x.currency != CURRENCY:
                other_currency += 1  # no euro amount: listed apart, in its own currency (I11)
                foreign.append(x)
                continue
            counted.append(x)
        total = sum((x.amount for x in counted), Decimal(0))
        by_kind: dict[str, Decimal] = defaultdict(Decimal)
        for x in counted:
            by_kind[x.kind] += x.amount
        span = (end - start).days
        group = group_by or ("month" if supplier_ids or span > 62 else "supplier")
        result = Money(start=start, end=end, direction=direction, lines=counted, total=total, group_by=group,
                       rows=self._rows(counted, group), by_kind=dict(by_kind), left_out=left_out, pending=pending,
                       private_count=private, other_currency=other_currency, covered=covered,
                       history_months=self._history_months(counted), waiting_payouts=waiting,
                       recharged=[x for x in clients if x.kind == "recharge"],
                       paid_back=[x for x in clients if x.kind != "recharge"], given_back=given_back,
                       claims_waiting=claims_waiting, disputed=disputed, held_for=held_for,
                       other_currency_lines=foreign, cash_banked=cash_banked, returned=returned)
        if compare is not None:
            p_start, p_end, p_label = compare
            result.previous = self.money(p_start, p_end, direction=direction, company_ids=company_ids,
                                         supplier_ids=supplier_ids, category=category, exclude=exclude,
                                         group_by=group)
            result.previous_label = p_label
        return result

    def _rows(self, lines: Sequence[Line], group: str) -> list[tuple[str, Decimal, int]]:
        sums: dict[str, list[Any]] = {}
        for x in lines:
            if group == "company":
                key = label = (self.repo.company_name(x.company_id) or "Not decided yet")
            elif group == "category":
                key = label = x.category_label
            elif group == "month":
                key = f"{x.on.year:04d}-{x.on.month:02d}"
                label = MONTH_NAMES[x.on.month - 1] if x.on.year == self.today.year else \
                    f"{MONTH_NAMES[x.on.month - 1]} {x.on.year}"
            else:
                key, label = x.supplier_id or x.merchant, x.merchant
            row = sums.setdefault(key, [label, Decimal(0), 0])
            row[1] += x.amount
            row[2] += 1
        if group == "month":
            return [(v[0], v[1], v[2]) for _, v in sorted(sums.items())]
        return [(v[0], v[1], v[2]) for v in sorted(sums.values(), key=lambda r: (-r[1], r[0]))]

    def vat(self, start: date, end: date, company_ids: Sequence[str] = ()) -> VatResult:
        """VAT on purchase and sales documents dated in the period, and VAT paid to the tax office."""
        repo = self.repo
        own_tax_ids = {fold(t) for t in repo.own_tax_ids()}
        purchases: list[dict[str, Any]] = []
        sales: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        for doc in sorted(repo.documents.values(), key=lambda d: (d.document.issue_date or date.min, d.id)):
            d = doc.document
            issued = d.issue_date or doc.received_at.date()
            if not start <= issued <= end or d.vat_amount is None or d.doc_type not in _PURCHASE_DOCS:
                continue
            company = d.entity_id or next((repo.transactions[t].company_id for t in doc.matched_tx_ids
                                           if t in repo.transactions and repo.transactions[t].tx.entity_id), None)
            sign = Decimal(-1) if d.doc_type is DocumentType.CREDIT_NOTE else Decimal(1)
            # A final invoice whose total includes advance invoices it takes off: their VAT was counted on them
            # already, so only the rest counts here (checklist X8: never twice).
            advances = [repo.documents[a].document for a in getattr(doc, "netted", {}) if a in repo.documents]
            taken_vat = sum((a.vat_amount or Decimal(0) for a in advances), Decimal(0))
            taken_gross = sum((abs(a.gross_amount or Decimal(0)) for a in advances), Decimal(0))
            entry = {"id": d.id, "supplier": display_name(d.supplier_name), "number": d.invoice_number or "",
                     "date": issued, "vat": (d.vat_amount - taken_vat) * sign,
                     "gross": ((d.gross_amount or Decimal(0)) - taken_gross) * sign,
                     "company": company, "onHold": doc.on_hold and not doc.hold_released,
                     "evidenceId": doc.evidence_ids[0] if doc.evidence_ids else None}
            is_sale = fold(d.supplier_tax_id or "") in own_tax_ids
            if company_ids and company not in company_ids:
                if company is None and not is_sale:
                    pending.append(entry)
                continue
            (sales if is_sale else purchases).append(entry)
        paid = [x for x in self.select(start=start, end=end, direction="out", company_ids=company_ids)
                if x.kind == "tax" and re.search(r"\b(?:iva|vat)\b", fold(f"{x.description}"))]
        return VatResult(start=start, end=end, purchases=purchases,
                         purchase_vat=sum((p["vat"] for p in purchases), Decimal(0)), sales=sales,
                         sales_vat=sum((s["vat"] for s in sales), Decimal(0)), paid_to_state=paid, pending=pending)

    def missing(self, *, start: date | None = None, end: date | None = None,
                company_ids: Sequence[str] = ()) -> list[tuple[Line, str]]:
        """Tracked payments that need a document and have none yet, with the plan for each (§22)."""
        repo = self.repo
        out = []
        for x in self.select(start=start, end=end, direction="out"):
            rec = repo.transactions.get(x.id)
            if rec is None or x.private or not x.needs_document or x.has_document:
                continue
            if repo.items[rec.item_id].is_done:
                continue
            if company_ids and x.company_id not in company_ids:
                continue
            out.append((x, self.svc.orchestrator.missing.plan(rec)))
        return out

    def missing_own(self, *, start: date | None = None, end: date | None = None,
                    company_ids: Sequence[str] = ()) -> list[tuple[Line, str]]:
        """Money in waiting for the business's own evidence: a member's fee without its receipt, cash paid into the
        bank without its till reports (checklist X12, X5). Never asked of the customer; the plan says what to do."""
        repo = self.repo
        orchestrator = self.svc.orchestrator
        out = []
        for x in self.select(start=start, end=end, direction="in"):
            rec = repo.transactions.get(x.id)
            if rec is None or x.private or rec.document_ids or repo.items[rec.item_id].is_done:
                continue
            if company_ids and x.company_id not in company_ids:
                continue
            plan = orchestrator.members.plan(rec) or orchestrator.cash.plan(rec)
            if plan:
                out.append((x, plan))
        return out

    def missing_reports(self, *, start: date | None = None, end: date | None = None,
                        company_ids: Sequence[str] = ()) -> list[tuple[Line, str]]:
        """Payouts still without a payout report that proves them, with the plan for each (§20, §22)."""
        repo = self.repo
        out = []
        for x in self.select(start=start, end=end, direction="in"):
            rec = repo.transactions.get(x.id)
            if rec is None or x.kind != "payout" or x.private or x.status == "settled":
                continue
            if repo.items[rec.item_id].is_done or (company_ids and x.company_id not in company_ids):
                continue
            out.append((x, self.svc.orchestrator.missing.plan(rec)))
        return out

    # -- presentation ---------------------------------------------------------

    def payment_label(self, x: Line) -> str:
        cash = " · paid in cash" if x.paid_in_cash else ""
        return f"{x.merchant} · {day_month(x.on, self.today)} · {format_money(x.amount, x.currency)}{cash}"

    def payment_dict(self, x: Line) -> dict[str, Any]:
        if x.history:
            invoice = "history"
        elif x.kind == "payout":
            invoice = "matched" if x.status == "settled" else "missing"  # its payout report
        elif x.kind in ("sales", "platform_fee", "till_cash"):
            invoice = "matched"  # read from the payout report that matched the bank, or the till report
        elif x.kind == "cash_deposit":
            invoice = "matched" if x.has_document else "missing"  # the till reports it comes from
        elif x.kind in RETURNED:
            invoice = "not needed"  # the two bank lines explain each other
        elif x.kind == "deposit":
            invoice = "matched" if x.has_document else "missing"  # your invoice for the work will take it off
        elif x.kind == "grant":
            invoice = "proof" if x.has_document else "missing"  # the grant letter, never an invoice (X30)
        elif not x.needs_document:
            invoice = "not needed"
        elif x.kind == "tax":
            invoice = "proof" if x.has_document else "missing"  # the tax letter or payment proof, not an invoice
        else:
            invoice = "matched" if x.has_document else "missing"
        out = {"id": x.evidence_id or "", "date": x.on.isoformat(), "label": x.merchant,
               "company": self.repo.company_name(x.company_id) or ("Not decided yet" if x.pending else ""),
               "amount": _num(x.amount), "invoice": invoice, "category": x.category_label}
        if x.paid_in_cash:
            out["paidWith"] = "cash"
        return out


def _num(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))
