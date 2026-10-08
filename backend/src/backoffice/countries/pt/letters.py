"""Letters to suppliers in Portuguese (§22, §23): the Portugal pack's ``supplier_letters()``.

:mod:`backoffice.missing.chase` decides what to ask, from facts only (amounts, dates, document numbers and our
company's details), and checks every message it sends; it asks the pack whose language a supplier reads to word
it. Each method returns the text in Portuguese: a subject and a body, or one line.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from backoffice.learning.plain import quantize_money

__all__ = ["LETTERS", "PortugueseLetters"]

MONTHS = (
    "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
)  # fmt: skip
_SYMBOLS = {"EUR": "€", "GBP": "£", "USD": "$"}
_SIGN_OFF = "Com os melhores cumprimentos"
_THANKS = "Agradecemos desde já."
_STATEMENT_WORDS = {"invoice": "Fatura", "credit_note": "Nota de crédito", "debit_note": "Nota de débito"}


class PortugueseLetters:
    """How a Portuguese letter to a supplier is worded."""

    language = "pt"
    months = MONTHS
    tax_id_label = "NIF"  # a Portuguese company's own tax number, named in its letters
    foreign_tax_id_label = "NIF/VAT"  # another country's company writing in Portuguese
    our_details_lead = "Os nossos dados"

    def money(self, amount: Decimal | int, currency: str = "EUR") -> str:
        """'1.234,56 €' (Portuguese separators, symbol after the amount)."""
        rounded = quantize_money(amount)
        whole, cents = f"{abs(rounded):,.2f}".split(".")
        digits = f"{whole.replace(',', '.')},{cents}"
        sign = "-" if rounded < 0 else ""
        code = currency.strip().upper()
        return f"{sign}{digits} {_SYMBOLS.get(code, code)}"

    def day_month(self, value: date, today: date | None = None) -> str:
        """'18 de setembro', or '18 de setembro de 2025' outside ``today``'s year."""
        text = f"{value.day} de {MONTHS[value.month - 1]}"
        if today is not None and value.year != today.year:
            text = f"{text} de {value.year}"
        return text

    def _close(self, details: str, company: str) -> str:
        return f"{_THANKS}\n\n{details}\n\n{_SIGN_OFF},\n{company}"

    def refund_request(self, money: str, when: str, details: str, company: str) -> tuple[str, str]:
        subject = f"Nota de crédito do reembolso de {money} de {when}"
        body = (
            f"Olá,\n\nRecebemos um reembolso de {money} a {when}. Poderiam, por favor, enviar-nos a nota de "
            f"crédito correspondente? {self._close(details, company)}"
        )
        return subject, body

    def request(self, number: str | None, money: str, when: str, details: str, company: str) -> tuple[str, str]:
        subject = f"Fatura {number}" if number else f"Fatura do pagamento de {money} de {when}"
        ask = (
            f"Poderiam, por favor, reenviar a fatura {number} referente ao pagamento de {money} de {when}?"
            if number
            else f"Poderiam, por favor, enviar-nos a fatura referente ao pagamento de {money} de {when}?"
        )
        return subject, f"Olá,\n\n{ask} {self._close(details, company)}"

    def reminder(self, refund: bool, number: str | None, money: str, when: str, details: str, company: str) -> str:
        if refund:
            return (
                f"Olá,\n\nRelembramos o nosso pedido: a nota de crédito do reembolso de {money} de {when}. "
                f"Poderiam enviá-la assim que possível? {self._close(details, company)}"
            )
        what = f"a fatura {number}" if number else "a fatura"
        return (
            f"Olá,\n\nRelembramos o nosso pedido: {what} referente ao pagamento de {money} de {when}. "
            f"Poderiam enviá-la assim que possível? {self._close(details, company)}"
        )

    def correction(self, number: str | None, money: str, when: str, details: str, company: str) -> tuple[str, str]:
        what = f"a fatura {number}" if number else "uma fatura"
        subject = f"Fatura {number}: pedido de fatura corrigida" if number else "Pedido de fatura corrigida"
        body = (
            f"Olá,\n\nRecebemos {what} de {money}, com data de {when}, e alguns dos seus dados não correspondem "
            "aos que temos registados. Não a vamos pagar tal como está.\n\n"
            "Poderiam, por favor, enviar-nos uma fatura corrigida, ou confirmar-nos que está correta? "
            f"{self._close(details, company)}"
        )
        return subject, body

    def statement_line(self, kind: str, number: str | None, on: date | None, amount: Decimal,
                       ours: Decimal | None, currency: str, today: date) -> str:
        word = _STATEMENT_WORDS.get(kind, _STATEMENT_WORDS["invoice"])
        number_text = f" {number}" if number else ""
        when = f" de {self.day_month(on, today)}" if on else ""
        money = self.money(amount, currency)
        if ours is not None:
            return (f"- {word}{number_text}{when}: no extrato {money}, na {word.lower()} que recebemos "
                    f"{self.money(ours, currency)}")
        return f"- {word}{number_text}{when}, {money}"

    def statement_request(self, corrections: bool, one: bool, listed: str, details: str,
                          company: str) -> tuple[str, str]:
        if corrections:
            subject = "Extrato de conta corrente: documentos com valores diferentes"
            corrected = "o documento corrigido" if one else "os documentos corrigidos"
            ask = ("O vosso extrato de conta corrente mostra valores diferentes dos documentos que recebemos:\n"
                   f"{listed}\n\nPoderiam, por favor, enviar-nos {corrected}, ou uma nota de crédito ou de débito "
                   "pela diferença?")
        else:
            subject = "Extrato de conta corrente: documentos em falta"
            these = "este documento, que" if one else "estes documentos, que"
            ask = (f"O vosso extrato de conta corrente indica {these} não recebemos:\n{listed}\n\n"
                   f"Poderiam, por favor, {'enviá-lo' if one else 'enviá-los'}?")
        return subject, f"Olá,\n\n{ask} {self._close(details, company)}"

    def recurring_request(self, year: int, month: int, usually_by: date, today: date, details: str,
                          company: str) -> tuple[str, str]:
        period = f"{MONTHS[month - 1]} de {year}"
        subject = f"Fatura de {period}"
        body = (
            f"Olá,\n\nA vossa fatura de {period} costuma chegar-nos até {self.day_month(usually_by, today)} e "
            f"ainda não a recebemos. Poderiam, por favor, enviá-la? {self._close(details, company)}"
        )
        return subject, body

    def link_request(self, emailed_on: date, today: date, details: str, company: str) -> tuple[str, str]:
        when = self.day_month(emailed_on, today)
        subject = f"Fatura do vosso email de {when}"
        body = (
            f"Olá,\n\nA ligação para a fatura no vosso email de {when} já não funciona. Poderiam, por favor, "
            f"enviar-nos a fatura em anexo? {self._close(details, company)}"
        )
        return subject, body


LETTERS = PortugueseLetters()
