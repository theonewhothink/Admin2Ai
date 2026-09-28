"""The demo tenant: Laura runs Hazel Tree, Company B and Company C from Lisbon.

:func:`build` sets up the companies, accounts, suppliers and connectors, then
replays September 2026 as it arrived (bank rows, emails, a scan, letters) through
the real orchestrator, up to Friday 2 October 2026, 09:30 Lisbon time. Every
figure the app shows afterwards (percent closed, what is handled, what needs
Laura) is computed by the pipeline from that evidence.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

from backoffice.domain.models import SourceKind, Supplier, Transaction
from backoffice.learning import counterparty_key
from backoffice.policy import ActionKind

from ..orchestrator import (
    Account,
    AccountantProfile,
    ConnectorState,
    Orchestrator,
    OwnerProfile,
    PortalDocument,
    Repository,
    local_datetime,
)
from . import evidence as E
from .evidence import K, row

TENANT = "demo-laura"
TODAY = date(2026, 10, 2)
NOW = local_datetime(TODAY, 9, 30)
START = local_datetime(date(2026, 9, 1), 7, 0)

OWNER = OwnerProfile(first_name="Laura", full_name="Laura Medina", email=E.OWNER_EMAIL)
ACCOUNTANT = AccountantProfile(id="acct-vidal", firm="Contabilidade Vidal", person="Marc Vidal",
                               email=E.ACCOUNTANT_EMAIL, software="TOConline")


def _setup(repo: Repository) -> None:
    repo.add_company(id="hazel-tree", name="Hazel Tree", legal_name="Hazel Tree Interiores, Lda.",
                     tax_id=E.HAZEL_NIF, ibans=[E.HAZEL_IBAN])
    repo.add_company(id="company-b", name="Company B", legal_name="Company B, Lda.", tax_id=E.COMPANY_B_NIF,
                     ibans=[E.COMPANY_B_IBAN])
    repo.add_company(id="company-c", name="Company C", legal_name="Company C Studio, Unipessoal Lda.",
                     tax_id=E.COMPANY_C_NIF, ibans=[E.COMPANY_C_IBAN])

    repo.add_account(Account(id="mbcp-ht", bank="Millennium BCP", holder_id="hazel-tree", iban=E.HAZEL_IBAN))
    repo.add_account(Account(id="mbcp-cc", bank="Millennium BCP", holder_id="company-c", iban=E.COMPANY_C_IBAN))
    repo.add_account(Account(id="cgd-b", bank="Caixa Geral de Depósitos", holder_id="company-b",
                             iban=E.COMPANY_B_IBAN))
    repo.add_account(Account(id="card-5530", bank="Millennium BCP", holder_id="hazel-tree", card_last4="5530"))
    repo.add_account(Account(id="card-7702", bank="Caixa Geral de Depósitos", holder_id="company-b",
                             card_last4="7702"))
    repo.add_account(Account(id="card-2291", bank="Millennium BCP", holder_id="company-c", card_last4="2291"))
    # Laura's own card, billed to Company C's account but used for all three companies.
    repo.add_account(Account(id="card-4817", bank="Millennium BCP", holder_id="company-c", card_last4="4817",
                             owned=False))

    t = TENANT
    repo.add_supplier(Supplier(id="sup-vodafone", tenant_id=t, name="Vodafone",
                               aliases=["VODAFONE PORTUGAL", "Vodafone Portugal"], tax_id=E.VODAFONE_NIF,
                               known_ibans=[E.VODAFONE_IBAN], email_domains=["vodafone.pt"], countries=["PT"],
                               contact_email="faturacao@vodafone.pt"),
                      phone="210 000 100")
    repo.add_supplier(Supplier(id="sup-edp", tenant_id=t, name="EDP", aliases=["EDP COMERCIAL", "EDP Comercial"],
                               tax_id=E.EDP_NIF, email_domains=["edp.pt"], countries=["PT"],
                               contact_email="faturas@edp.pt"))
    repo.add_supplier(Supplier(id="sup-ikea", tenant_id=t, name="IKEA", aliases=["IKEA ALFRAGIDE", "IKEA Portugal"],
                               tax_id=E.IKEA_NIF, countries=["PT"]))
    repo.add_supplier(Supplier(id="sup-uber", tenant_id=t, name="Uber", aliases=["UBER TRIP", "UBER *TRIP"],
                               tax_id=E.UBER_NIF, email_domains=["uber.com"], countries=["PT"]))
    repo.add_supplier(Supplier(id="sup-adobe", tenant_id=t, name="Adobe", aliases=["ADOBE CREATIVE CLOUD"],
                               tax_id=E.ADOBE_NIF, email_domains=["adobe.com"], countries=["PT"]))
    repo.add_supplier(Supplier(id="sup-landlord", tenant_id=t, name="Marta Gonçalves", aliases=["MARTA GONCALVES"],
                               tax_id=E.LANDLORD_NIF, known_ibans=[E.LANDLORD_IBAN], countries=["PT"]))
    repo.add_supplier(Supplier(id="sup-predial", tenant_id=t, name="Predial Alfama", aliases=["PREDIAL ALFAMA LDA"],
                               tax_id=E.PREDIAL_NIF, known_ibans=[E.PREDIAL_IBAN], countries=["PT"]))

    covered_from = local_datetime(date(2026, 6, 1), 0, 0)
    repo.add_connector(ConnectorState(
        id="gmail", name="Gmail", kind="email", account=E.OWNER_EMAIL,
        company_ids=("hazel-tree", "company-b", "company-c"), healthy=True, covered_from=covered_from,
        covered_until=local_datetime(TODAY, 9, 12), last_synced_at=local_datetime(TODAY, 9, 12)))
    repo.add_connector(ConnectorState(
        id="millennium", name="Millennium BCP", kind="bank", account="Hazel Tree · Company C",
        company_ids=("hazel-tree", "company-c"), healthy=True, covered_from=covered_from,
        covered_until=local_datetime(TODAY, 8, 55), last_synced_at=local_datetime(TODAY, 8, 55)))
    repo.add_connector(ConnectorState(
        id="cgd", name="Caixa Geral de Depósitos", kind="bank", account="Company B", company_ids=("company-b",),
        healthy=True, covered_from=covered_from, covered_until=local_datetime(TODAY, 8, 55),
        last_synced_at=local_datetime(TODAY, 8, 55)))
    repo.add_connector(ConnectorState(
        id="accountant", name=ACCOUNTANT.firm, kind="accountant", account=ACCOUNTANT.email,
        company_ids=("hazel-tree", "company-b", "company-c"), healthy=True, covered_from=covered_from,
        covered_until=local_datetime(date(2026, 10, 1), 18, 20),
        last_synced_at=local_datetime(date(2026, 10, 1), 18, 20)))
    repo.accountant = ACCOUNTANT

    # What Laura allowed at onboarding (§25 "automatic if authorized").
    granted = START - timedelta(days=90)
    repo.policy = repo.policy.with_grant(ActionKind.SUPPLIER_INVOICE_REQUEST, granted_by=E.OWNER_EMAIL, at=granted)
    repo.policy = repo.policy.with_grant(ActionKind.ROUTINE_ACCOUNTANT_RESPONSE, granted_by=E.OWNER_EMAIL,
                                         at=granted)

    # Supplier portal adapters (§10): the invoice behind Adobe's "View invoice" button.
    repo.portal[E.ADOBE_INVOICE_URL] = PortalDocument(data=E.ADOBE_INVOICE, filename="FT_AD2026_7734.txt",
                                                      content_type="text/plain", portal="Adobe account")

    # The 90-day history import (§6): earlier payments, used only for learning.
    history = [
        ("card-2291", date(2026, 6, 22), "-54.99", "ADOBE *CREATIVE CLOUD", "2291"),
        ("card-2291", date(2026, 7, 22), "-54.99", "ADOBE *CREATIVE CLOUD", "2291"),
        ("card-2291", date(2026, 8, 22), "-59.99", "ADOBE *CREATIVE CLOUD", "2291"),
        ("mbcp-ht", date(2026, 6, 2), "-92.40", "VODAFONE PORTUGAL", None),
        ("mbcp-ht", date(2026, 7, 2), "-92.40", "VODAFONE PORTUGAL", None),
        ("mbcp-ht", date(2026, 8, 2), "-92.40", "VODAFONE PORTUGAL", None),
        ("card-4817", date(2026, 7, 11), "-126.50", "IKEA ALFRAGIDE", "4817"),
        ("card-4817", date(2026, 8, 19), "-89.90", "IKEA ALFRAGIDE", "4817"),
    ]
    for i, (account, day, amount, who, card) in enumerate(history):
        repo.history_transactions.append(Transaction(
            id=f"hist_{i:02d}", tenant_id=t, account_id=account, booked_on=day, amount=Decimal(amount),
            counterparty=who, card_last4=card, entity_id=repo.accounts[account].holder_id if card != "4817" else None))
    # Laura put both earlier IKEA orders on Hazel Tree when she answered at onboarding.
    repo.history_pairs += [(counterparty_key("IKEA ALFRAGIDE") or "ikea", "hazel-tree")] * 2


@dataclass(frozen=True)
class _Event:
    at: datetime
    run: Callable[[Orchestrator, datetime], object]


def _bank(*rows):  # type: ignore[no-untyped-def]
    return lambda o, at: o.ingest_bank(list(rows), at=at)


def _mail(build):  # type: ignore[no-untyped-def]
    return lambda o, at: o.ingest_file(build(at), filename="message.eml", content_type="message/rfc822",
                                       source_kind=SourceKind.EMAIL, at=at, origin="email")


def _file(data: bytes, filename: str, content_type: str, source: SourceKind = SourceKind.UPLOAD,
          origin: str = "upload"):  # type: ignore[no-untyped-def]
    return lambda o, at: o.ingest_file(data, filename=filename, content_type=content_type, source_kind=source,
                                       at=at, origin=origin)


def _events() -> list[_Event]:
    d = lambda m, day, h, mi=0: local_datetime(date(2026, m, day), h, mi)  # noqa: E731
    return [
        _Event(d(9, 1, 8, 5), _bank(
            row("mbcp-0901-01", "mbcp-ht", date(2026, 9, 1), "-1200.00", "MARTA GONCALVES", "TRF RENDA SETEMBRO",
                K.TRANSFER_OUT, iban=E.LANDLORD_IBAN),
            row("cgd-0901-01", "cgd-b", date(2026, 9, 1), "-950.00", "PREDIAL ALFAMA LDA", "TRF RENDA ESCRITORIO",
                K.TRANSFER_OUT, iban=E.PREDIAL_IBAN))),
        _Event(d(9, 1, 10, 14), _mail(E.landlord_email)),
        _Event(d(9, 1, 11, 2), _mail(E.vodafone_september_email)),
        _Event(d(9, 1, 16, 40), _file(E.PREDIAL_RECEIPT, "Recibo_FR_PA2026_211.txt", "text/plain")),
        _Event(d(9, 2, 7, 0), _bank(
            row("mbcp-0902-01", "mbcp-ht", date(2026, 9, 2), "-92.40", "VODAFONE PORTUGAL", "DD VODAFONE PORTUGAL",
                K.DIRECT_DEBIT))),
        _Event(d(9, 5, 19, 30), _bank(
            row("cgd-0905-01", "card-7702", date(2026, 9, 5), "-23.40", "UBER *TRIP", "COMPRA CARTAO", K.CARD,
                card="7702"))),
        _Event(d(9, 5, 19, 42), _mail(lambda at: E.uber_email(at, E.UBER_B_RECEIPT, "Company B", "FS UBR2026/45077"))),
        _Event(d(9, 10, 9, 0), _file(E.AT_LETTER_HAZEL, "Carta_AT_IVA_2026-07.txt", "text/plain")),
        _Event(d(9, 15, 18, 5), _bank(
            row("mbcp-0915-01", "card-5530", date(2026, 9, 15), "-18.75", "UBER *TRIP", "COMPRA CARTAO", K.CARD,
                card="5530"))),
        _Event(d(9, 15, 18, 20), _mail(lambda at: E.uber_email(at, E.UBER_HT_RECEIPT, "Hazel Tree",
                                                                "FS UBR2026/48213"))),
        _Event(d(9, 19, 7, 0), _bank(
            row("mbcp-0919-01", "mbcp-ht", date(2026, 9, 19), "-64.10", "EDP COMERCIAL", "DD EDP COMERCIAL",
                K.DIRECT_DEBIT))),
        _Event(d(9, 21, 10, 30), _bank(
            row("mbcp-0921-01", "mbcp-ht", date(2026, 9, 21), "-2184.37", "AUTORIDADE TRIBUTARIA",
                "PAG ESTADO IVA 2026/07", K.TRANSFER_OUT, reference="161204587"))),
        _Event(d(9, 22, 13, 12), _bank(
            row("mbcp-0922-01", "card-2291", date(2026, 9, 22), "-59.99", "ADOBE *CREATIVE CLOUD", "COMPRA CARTAO",
                K.CARD, card="2291"))),
        _Event(d(9, 25, 9, 0), _bank(
            row("mbcp-0925-01", "mbcp-ht", date(2026, 9, 25), "-500.00", "COMPANY C STUDIO", "TRF COMPANY C",
                K.TRANSFER_OUT, iban=E.COMPANY_C_IBAN),
            row("mbcp-0925-02", "mbcp-cc", date(2026, 9, 25), "500.00", "HAZEL TREE INTERIORES", "TRF HAZEL TREE",
                K.TRANSFER_IN, iban=E.HAZEL_IBAN))),
        _Event(d(9, 29, 17, 48), _bank(
            row("mbcp-0929-01", "card-4817", date(2026, 9, 29), "-418.00", "IKEA ALFRAGIDE", "COMPRA CARTAO", K.CARD,
                card="4817"))),
        _Event(d(9, 29, 18, 10), _file(E.IKEA_RECEIPT, "IKEA_talao.txt", "text/plain", SourceKind.MOBILE_SCAN,
                                       origin="scan")),
        _Event(d(9, 30, 23, 0), _bank(
            row("mbcp-0930-01", "mbcp-ht", date(2026, 9, 30), "-6.24", "MILLENNIUM BCP", "COMISSAO MANUTENCAO CONTA",
                K.FEE),
            row("cgd-0930-01", "cgd-b", date(2026, 9, 30), "-4.16", "CAIXA GERAL DEPOSITOS",
                "IMPOSTO DO SELO COMISSAO", K.FEE),
            row("mbcp-0930-02", "mbcp-cc", date(2026, 9, 30), "-3.12", "MILLENNIUM BCP", "COMISSAO MANUTENCAO CONTA",
                K.FEE))),
        _Event(d(9, 30, 18, 2), _file(E.AT_LETTER_COMPANY_B, "Carta_AT_retencoes_2026-09.txt", "text/plain")),
        _Event(d(10, 1, 8, 30), _mail(E.accountant_email)),
        _Event(d(10, 1, 9, 12), _mail(E.vodafone_october_email)),
        _Event(d(10, 2, 8, 30), _mail(E.adobe_email)),
    ]


def build() -> Orchestrator:
    """The demo tenant with September replayed through the pipeline, as of 2 October 2026, 09:30."""
    repo = Repository(tenant_id=TENANT, owner=OWNER, now=START)
    _setup(repo)
    orchestrator = Orchestrator(repo)
    for event in sorted(_events(), key=lambda e: e.at):
        event.run(orchestrator, event.at)
    orchestrator.run(NOW)
    return orchestrator
