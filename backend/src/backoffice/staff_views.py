"""The API around employee cards and staff expenses (backoffice.staff), as plain JSON-able dicts.

For the owner: ``GET/POST /api/employees``, ``POST /api/employees/<id>`` (name, email, phone, company,
cards), ``GET/POST /api/expense-claims`` (a receipt an employee paid, sent on their behalf).

For an employee (the production ``employee`` role, backoffice.server.auth): only their own open card
payments (``GET /api/employee/card-payments``) and their receipt uploads (``POST /api/employee/receipts``,
``paidPersonally: true`` for an expense claim). Nothing else about the business is in these replies.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from backoffice.domain.models import SourceKind
from backoffice.fraud import normalize_iban
from backoffice.fraud.iban import is_valid_iban
from backoffice.learning import day_month, format_money
from backoffice.staff import CLAIM_STATUS_LABELS, Employee, plain_email

if TYPE_CHECKING:  # pragma: no cover
    from backoffice.service import BackOfficeService

__all__ = ["StaffViews"]

MAX_UPLOAD = 25 * 1024 * 1024


def _num(value: Any) -> float | None:
    return None if value is None else float(value)


class StaffViews:
    def __init__(self, svc: BackOfficeService) -> None:
        self.svc = svc
        self.repo = svc.repo
        self.o = svc.orchestrator
        self.staff = svc.orchestrator.staff

    @staticmethod
    def _error(status: int, message: str) -> Exception:
        from backoffice.service import ServiceError

        return ServiceError(status, message)

    # ----------------------------------------------------------------- the owner: employees and their cards

    def _employee_json(self, emp: Employee) -> dict[str, Any]:
        repo = self.repo
        waiting = self.staff.open_card_payments(emp)
        asked = [r for r in waiting if (q := repo.receipt_requests.get(r.id)) is not None and q.asked]
        claims = [c for c in repo.expense_claims.values() if c.employee_id == emp.id]
        owed = sum((c.amount for c in claims if c.status == "approved"), start=0)
        return {
            "id": emp.id, "name": emp.name, "email": emp.email, "phone": emp.phone, "companyId": emp.company_id,
            "companyName": repo.company_name(emp.company_id) or "",
            "cards": [{"last4": c, "label": f"card •••• {c}"} for c in emp.cards],
            "learnedFromBank": bool(emp.learned_from), "receiptsMissing": len(waiting), "receiptsAsked": len(asked),
            "claimsWaiting": sum(1 for c in claims if c.status == "waiting"), "toPayBack": _num(owed),
        }

    def employees(self) -> dict[str, Any]:
        return {"employees": [self._employee_json(e) for e in sorted(self.repo.employees.values(),
                                                                      key=lambda e: (e.name.lower(), e.id))]}

    def save(self, body: Mapping[str, Any] | None, employee_id: str | None = None) -> dict[str, Any]:
        """Add an employee, or change one: name, email, phone, company, cards (last 4 digits), bank account."""
        body = body or {}
        existing = self.repo.employees.get(employee_id or "") if employee_id else None
        if employee_id and existing is None:
            raise self._error(404, "I can't find that person.")
        name = " ".join(str(body.get("name") or (existing.name if existing else "")).split())[:120]
        if not name:
            raise self._error(400, "What is their name?")
        email = body.get("email")
        if email is not None:
            email = str(email).strip().lower()
            if email and not plain_email(email):
                raise self._error(400, "That doesn't look like an email address.")
            other = self.staff.employee_by_email(email) if email else None
            if other is not None and other is not existing:
                raise self._error(409, f"{email} is already {other.name}'s email.")
        phone = body.get("phone")
        if phone is not None:
            phone = " ".join(str(phone).split())[:40]
            if phone and not re.fullmatch(r"\+?[0-9 ()-]{6,40}", phone):
                raise self._error(400, "That doesn't look like a phone number.")
        company = body.get("companyId")
        if company is not None:
            company = str(company)
            if company and company not in self.repo.companies:
                raise self._error(404, "I can't find that company.")
        raw_cards = body.get("cards", body.get("card"))
        cards: list[str] | None = None  # given: exactly the cards they hold; not given: as before
        if raw_cards is not None:
            cards = []
            for c in raw_cards if isinstance(raw_cards, list) else [raw_cards]:
                last4 = str(c).strip()[-4:]
                if not re.fullmatch(r"\d{4}", last4):
                    raise self._error(400, "A card is its last 4 digits.")
                cards.append(last4)
        iban = body.get("iban")
        if iban is not None:
            iban = normalize_iban(str(iban)) if iban else ""
            if iban and not is_valid_iban(iban):
                raise self._error(400, "That IBAN doesn't look right. Check the digits.")
        emp = self.staff.set_employee(name=name, email=email, phone=phone, company_id=company,
                                      cards=tuple(cards) if cards is not None else None, iban=iban,
                                      employee_id=existing.id if existing else None)
        self.o.run()
        if emp.cards and plain_email(emp.email):
            cards_text = ", ".join(f"•••• {c}" for c in emp.cards)
            message = (f"Done. When a payment on card {cards_text} misses its receipt, I will ask "
                       f"{emp.first_name}, not you.")
        elif emp.cards:
            message = f"Done. Add {emp.first_name}'s email so I can ask {emp.first_name} for receipts."
        else:
            message = f"Done. {emp.name} can now send receipts they paid themselves."
        return {"ok": True, "employee": self._employee_json(emp), "message": message}

    # ----------------------------------------------------------------- expense claims

    def _claim_json(self, claim: Any, *, for_employee: bool = False) -> dict[str, Any]:
        out = {"id": claim.id, "merchant": claim.merchant, "amount": _num(claim.amount), "currency": claim.currency,
               "date": claim.spent_on.isoformat(), "status": claim.status,
               "statusLabel": CLAIM_STATUS_LABELS.get(claim.status, "")}
        if for_employee:
            return out
        emp = self.repo.employees.get(claim.employee_id)
        needs = self.repo.needs.get(claim.needs_id or "")
        out.update({"employeeId": claim.employee_id, "employee": emp.name if emp else "",
                    "companyId": claim.company_id, "companyName": self.repo.company_name(claim.company_id) or "",
                    "evidenceIds": list(claim.evidence_ids), "paidBy": claim.paid_by_tx_id,
                    "needsId": needs.id if needs is not None and needs.status == "open" else None,
                    "note": self.staff.claim_status_line(claim, you=False)})
        return out

    def claims(self) -> dict[str, Any]:
        return {"claims": [self._claim_json(c) for c in sorted(self.repo.expense_claims.values(),
                                                                key=lambda c: (c.spent_on, c.id), reverse=True)]}

    def owner_claim(self, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """The owner sends a receipt an employee paid themselves, on their behalf."""
        body = body or {}
        emp = self._employee(body)
        return self.receipt(body, emp, submitted_by="owner", paid_personally=True)

    def _employee(self, body: Mapping[str, Any]) -> Employee:
        emp = self.repo.employees.get(str(body.get("employeeId") or "")) or \
            self.staff.employee_by_email(str(body.get("employeeEmail") or body.get("employee") or ""))
        if emp is None:
            raise self._error(404, "I can't find that person. Add them first.")
        return emp

    # ----------------------------------------------------------------- what an employee sees and sends

    def card_payments(self, email_or_id: str | None) -> dict[str, Any]:
        """One employee's own card payments still waiting for a receipt, and their expense claims. Nothing else."""
        emp = self.repo.employees.get(email_or_id or "") or self.staff.employee_by_email(email_or_id)
        if emp is None:
            raise self._error(404, "I can't find you in this business's team yet. Ask the owner to add you.")
        today = self.svc._today()
        payments = []
        for rec in self.staff.open_card_payments(emp):
            req = self.repo.receipt_requests.get(rec.id)
            merchant = self.o.merchant_name(rec.tx)
            amount = format_money(abs(rec.tx.amount), rec.tx.currency)
            payments.append({
                "id": rec.id, "merchant": merchant, "amount": _num(abs(rec.tx.amount)), "currency": rec.tx.currency,
                "date": rec.tx.booked_on.isoformat(), "card": f"card •••• {rec.tx.card_last4}",
                "status": "asked" if req is not None and req.asked else "open",
                "note": f"Please send the receipt for {amount} at {merchant} on {day_month(rec.tx.booked_on, today)}."})
        claims = [self._claim_json(c, for_employee=True)
                  for c in sorted(self.repo.expense_claims.values(), key=lambda c: (c.spent_on, c.id), reverse=True)
                  if c.employee_id == emp.id]
        return {"name": emp.name, "payments": payments, "claims": claims}

    def employee_receipt(self, body: Mapping[str, Any] | None) -> dict[str, Any]:
        """``POST /api/employee/receipts``: an employee's receipt (the server sets ``employeeEmail`` from the
        signed-in person), or the owner sending one for them (``employeeId``)."""
        body = body or {}
        emp = self._employee(body)
        by_employee = bool(body.get("employeeEmail")) and not body.get("employeeId")
        return self.receipt(body, emp, submitted_by="employee" if by_employee else "owner",
                            paid_personally=bool(body.get("paidPersonally")))

    def receipt(self, body: Mapping[str, Any], emp: Employee, *, submitted_by: str,
                paid_personally: bool) -> dict[str, Any]:
        from backoffice.service import _b64

        data = _b64(body.get("dataBase64") or body.get("data_base64"))
        if not data:
            raise self._error(400, "There was nothing to save.")
        if len(data) > MAX_UPLOAD:
            raise self._error(413, "This file is too large to send.")
        filename = body.get("filename") if isinstance(body.get("filename"), str) else None
        content_type = body.get("contentType") or body.get("content_type")
        content_type = content_type if isinstance(content_type, str) else None
        if (filename or "").lower().endswith(".csv") or (content_type or "").startswith("text/csv"):
            raise self._error(415, "Send a photo or a PDF of the receipt.")
        company = body.get("companyId")
        if company and str(company) not in self.repo.companies:
            raise self._error(404, "I can't find that company.")
        you = submitted_by == "employee"
        report = self.o.ingest_file(data, filename=filename, content_type=content_type,
                                    source_kind=SourceKind.MOBILE_SCAN if you else SourceKind.UPLOAD,
                                    origin="scan" if you else "upload", run=not paid_personally)
        if report.route == "unsupported":
            raise self._error(415, report.message)
        docs = [self.repo.documents[d] for d in dict.fromkeys(report.document_ids) if d in self.repo.documents]
        out: dict[str, Any] = {"ok": True}
        if paid_personally:
            if not docs:
                self.o.run()
                out["message"] = "Got it. I stored it, but I couldn't read the amount on it yet."
                return out
            claim, message = self.staff.claim(emp, docs[0], company_id=str(company) if company else None,
                                              submitted_by=submitted_by, now=self.svc._now())
            self.o.run()
            out["message"] = message
            if claim is not None:
                out["claim"] = self._claim_json(claim, for_employee=you)
            return out
        if not docs:
            out["message"] = "Got it. I stored it, but I couldn't read it yet."
            return out
        doc = docs[0]
        target = None
        if not doc.matched_tx_ids and doc.claim_id is None:
            mine = self.staff.open_card_payments(emp)
            chosen = str(body.get("paymentId") or "")
            fitting = [r for r in mine if (not chosen or r.id == chosen) and self.staff.fits(r, doc)]
            if len(fitting) == 1:
                target = fitting[0]
                self.staff.link(target, doc, emp, "sent by them" if you else "sent for them")
                self.o.run()
        matched = [self.repo.transactions[t] for t in doc.matched_tx_ids if t in self.repo.transactions]
        rec = target or (matched[0] if matched else None)
        if rec is not None and rec.tx.card_last4 in emp.cards:
            amount = format_money(abs(rec.tx.amount), rec.tx.currency)
            whose = "your" if you else f"{emp.first_name}'s"
            out["message"] = (f"Got it. It matches {whose} {amount} card payment at {self.o.merchant_name(rec.tx)} "
                              f"on {day_month(rec.tx.booked_on, self.svc._today())}.")
            out["paymentId"] = rec.id
        elif rec is not None:
            out["message"] = "Got it. Thank you."
        else:
            out["message"] = "Got it. I'm matching it with your card payments." if you else \
                f"Got it. I'm matching it with {emp.first_name}'s card payments."
        return out

    # ----------------------------------------------------------------- a card added with its holder

    def holder(self, body: Mapping[str, Any]) -> tuple[Employee | None, dict[str, Any]]:
        """Who holds a card being added (``holderName`` / ``holderEmail`` / ``holderPhone``), checked before
        anything changes: the person already on file (by email, else by name), and what to save for them."""
        name = " ".join(str(body.get("holderName") or "").split())[:120]
        email = str(body.get("holderEmail") or "").strip().lower()
        if email and not plain_email(email):
            raise self._error(400, "That doesn't look like an email address.")
        existing = self.staff.employee_by_email(email) if email else None
        if existing is None and name:
            same = [e for e in self.repo.employees.values() if e.name.lower() == name.lower()]
            existing = same[0] if len(same) == 1 and (not email or not same[0].email) else None
        name = name or (existing.name if existing is not None else "")
        if not name:
            raise self._error(400, "Who holds this card?")
        payload: dict[str, Any] = {"name": name}
        if email:
            payload["email"] = email
        if body.get("holderPhone"):
            payload["phone"] = body.get("holderPhone")
        return existing, payload

    def card_holder(self, account_id: str, last4: str, body: Mapping[str, Any]) -> str:
        """``POST /api/sources`` with ``kind: card`` and a holder: the card is theirs. Returns what to tell the
        owner."""
        existing, payload = self.holder(body)
        account = self.repo.accounts.get(account_id)
        payload["cards"] = [*(existing.cards if existing is not None else ()), last4]
        if account is not None and (existing is None or not existing.company_id):
            payload["companyId"] = account.holder_id
        return self.save(payload, existing.id if existing else None)["message"]
