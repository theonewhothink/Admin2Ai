"""FX reference rates and bank-declared FX metadata (§20 FX)."""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

import httpx
import pytest

from backoffice.domain.models import Transaction, TransactionKind
from backoffice.reconciliation import (
    BankMetadata,
    EcbConfig,
    EcbFxRates,
    FxDetails,
    FxRateSource,
    FxRateUnavailable,
    MatchContext,
    StaticFxRates,
    fx_from_text,
    is_card_settlement,
)

D = Decimal
CSV_COLUMNS = [
    "KEY", "FREQ", "CURRENCY", "CURRENCY_DENOM", "EXR_TYPE", "EXR_SUFFIX",
    "TIME_PERIOD", "OBS_VALUE", "OBS_STATUS",
]  # fmt: skip
CSV_HEADER = ",".join(CSV_COLUMNS) + "\n"


def ecb_csv(currency: str, rows: list[tuple[str, str]]) -> str:
    body = "".join(
        f"EXR.D.{currency}.EUR.SP00.A,D,{currency},EUR,SP00,A,{day},{value},A\n"
        for day, value in rows
    )
    return CSV_HEADER + body


class FakeEcb:
    """Records requests and answers like the ECB data portal."""

    def __init__(
        self, series: dict[str, list[tuple[str, str]]], status: int = 200
    ) -> None:
        self.series = series
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="")
        currency = request.url.path.rsplit("/", 1)[-1].split(".")[1]
        rows = self.series.get(currency)
        if rows is None:
            return httpx.Response(404, text="No results found.")
        return httpx.Response(200, text=ecb_csv(currency, rows))


def ecb(fake: FakeEcb) -> EcbFxRates:
    return EcbFxRates(
        EcbConfig(base_url="https://ecb.test/service/data/EXR"),
        client=httpx.Client(transport=httpx.MockTransport(fake)),
    )


# --- static rates


def test_static_rates_invert_and_short_circuit() -> None:
    rates = StaticFxRates({("USD", "EUR"): D("0.92")})
    assert rates.rate("usd", "eur", date(2026, 9, 1)) == D("0.92")
    assert rates.rate("EUR", "USD", date(2026, 9, 1)).quantize(D("0.0001")) == D(
        "1.0870"
    )
    assert rates.rate("GBP", "GBP", date(2026, 9, 1)) == 1
    assert rates.rate("GBP", "EUR", date(2026, 9, 1)) is None
    assert isinstance(rates, FxRateSource)


def test_static_rates_refuse_floats() -> None:
    with pytest.raises(ValueError):
        StaticFxRates({("USD", "EUR"): 0.92})  # type: ignore[dict-item]


# --- ECB adapter


def test_ecb_picks_latest_rate_on_or_before_the_day_and_caches() -> None:
    fake = FakeEcb(
        {
            "USD": [
                ("2026-09-24", "1.0820"),
                ("2026-09-25", "1.0850"),
                ("2026-09-28", "1.2000"),
            ]
        }
    )
    rates = ecb(fake)
    saturday = date(2026, 9, 26)
    usd_per_eur = rates.rate("EUR", "USD", saturday)
    assert usd_per_eur == D("1.0850")
    assert rates.rate("USD", "EUR", saturday) == D(1) / D("1.0850")
    assert len(fake.requests) == 1  # cached per (currency, day)
    params = fake.requests[0].url.params
    assert params["startPeriod"] == "2026-09-19" and params["endPeriod"] == "2026-09-26"
    assert params["format"] == "csvdata"
    assert fake.requests[0].url.path.endswith("/EXR/D.USD.EUR.SP00.A")


def test_ecb_cross_rates_go_through_the_euro() -> None:
    fake = FakeEcb(
        {"USD": [("2026-09-25", "1.0850")], "GBP": [("2026-09-25", "0.8400")]}
    )
    rate = ecb(fake).rate("USD", "GBP", date(2026, 9, 25))
    assert rate.quantize(D("0.000001")) == (D("0.8400") / D("1.0850")).quantize(
        D("0.000001")
    )


def test_ecb_without_data_returns_none() -> None:
    rates = ecb(FakeEcb({}))
    assert rates.rate("XAU", "EUR", date(2026, 9, 25)) is None


def test_ecb_server_errors_are_reported_as_unavailable() -> None:
    with pytest.raises(FxRateUnavailable):
        ecb(FakeEcb({}, status=503)).rate("USD", "EUR", date(2026, 9, 25))


def test_ecb_transport_errors_are_reported_as_unavailable() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    rates = EcbFxRates(client=httpx.Client(transport=httpx.MockTransport(broken)))
    with pytest.raises(FxRateUnavailable):
        rates.rate("USD", "EUR", date(2026, 9, 25))


def test_ecb_unexpected_format_is_reported_as_unavailable() -> None:
    def html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    rates = EcbFxRates(client=httpx.Client(transport=httpx.MockTransport(html)))
    with pytest.raises(FxRateUnavailable):
        rates.rate("USD", "EUR", date(2026, 9, 25))


def test_match_context_degrades_gracefully_when_rates_are_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    context = MatchContext(fx_rates=ecb(FakeEcb({}, status=500)))
    with caplog.at_level(logging.WARNING):
        assert context.reference_rate("USD", "EUR", date(2026, 9, 25)) is None
    assert any("unavailable" in r.getMessage() for r in caplog.records)


# --- bank metadata


def test_fx_details_validate_money() -> None:
    with pytest.raises(TypeError):
        FxDetails(100.0, "USD")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FxDetails(D("-1"), "USD")
    with pytest.raises(ValueError):
        FxDetails(D("1"), "US")
    with pytest.raises(ValueError):
        FxDetails(D("1"), "USD", fee=D("-0.10"))
    assert FxDetails(D("1"), "usd").original_currency == "USD"


def test_bank_metadata_validation_and_total_fee() -> None:
    meta = BankMetadata(fee=D("1.50"), fx=FxDetails(D("10"), "USD", fee=D("0.25")))
    assert meta.total_fee == D("1.75")
    with pytest.raises(ValueError):
        BankMetadata(fee=D("-1"))
    with pytest.raises(ValueError):
        BankMetadata(settlement_period=(date(2026, 9, 2), date(2026, 9, 1)))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("COMPRA AMAZON.COM USD 100,00 TAXA 0,9215", (D("100.00"), "USD", D("0.9215"))),
        ("ORIG AMT 1,234.56 USD RATE 0.9217", (D("1234.56"), "USD", D("0.9217"))),
        ("NETFLIX 15.99 USD @ 0.93", (D("15.99"), "USD", D("0.93"))),
        ("GBP 1.234,50", (D("1234.50"), "GBP", None)),
    ],
)
def test_fx_amount_is_read_from_bank_text(text: str, expected: tuple) -> None:
    fx = fx_from_text(text, "EUR")
    assert (
        fx is not None
        and (fx.original_amount, fx.original_currency, fx.rate) == expected
    )


@pytest.mark.parametrize(
    "text", ["EUR 10.00", "USD 10 GBP 5", "100,00 TAXA", "", "TAX 5"]
)
def test_fx_text_parsing_refuses_to_guess(text: str) -> None:
    assert fx_from_text(text, "EUR") is None


def _tx(
    counterparty: str,
    kind: TransactionKind,
    amount: str = "-100",
    card: str | None = None,
) -> Transaction:
    return Transaction(
        tenant_id="t",
        account_id="a",
        booked_on=date(2026, 9, 1),
        amount=D(amount),
        counterparty=counterparty,
        kind=kind,
        card_last4=card,
    )


def test_card_settlement_detection() -> None:
    assert is_card_settlement(
        _tx("LIQUIDACAO CARTAO CREDITO", TransactionKind.DIRECT_DEBIT)
    )
    assert is_card_settlement(
        _tx("CREDIT CARD PAYMENT", TransactionKind.CARD)
    )  # kind defaulted, no card number
    assert not is_card_settlement(
        _tx("CREDIT CARD PAYMENT", TransactionKind.CARD, card="4817")
    )
    assert not is_card_settlement(
        _tx("LIQUIDACAO CARTAO", TransactionKind.TRANSFER_IN, amount="100")
    )
    assert not is_card_settlement(_tx("VODAFONE", TransactionKind.DIRECT_DEBIT))
    assert is_card_settlement(
        _tx("DEBITO", TransactionKind.DIRECT_DEBIT),
        BankMetadata(settles_card_last4="4817"),
    )
