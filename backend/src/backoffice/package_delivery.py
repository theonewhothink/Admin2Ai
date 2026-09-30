"""The monthly accountant package, sent on schedule (§27 Day 0 / Day +1, §28; checklist N9, N10).

On the working day the owner chose after month-end (the report settings, working day 3 by default;
weekends and Portuguese public holidays do not count), each company's month goes to that company's
accountant: the booked documents, supporting documents marked as such, ``ledger.csv``,
``evidence_index.csv``, ``manifest.json`` and the untouched originals (closure/package.py), as one ZIP
attached to an email written through the send path. The originals are left out, and the email says so,
when they would make the attachment too large; the accountant then downloads them from their workspace.

Honest by construction (§3):

* the package counts as *delivered* only once a transport accepted the email (``PackageAgent.sent``),
  never when it was written;
* it is *confirmed* only by the accountant's reply in that email's thread (In-Reply-To / References
  naming the package's Message-ID), from the address it went to or that address's own domain;
* a month with items still open is sent on the day with a plain note of what is still open, or waits
  until it closes when the owner chose so (``whenOpen: "wait"``). When a month sent incomplete closes
  later, the final package follows once;
* sending is the §25 "document delivery" action: it runs on its own only when the owner allowed it
  (naming the accountant does; the owner's automation settings can switch it off). A package written
  while it was allowed and switched off before it went is held back (Orchestrator.held_back).

The owner reads "September sent to your accountant on 6 October." only when that is true.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from backoffice.closure import Month
from backoffice.closure.package import (
    AccountantPackage,
    DeliveryChannel,
    DeliveryError,
    DeliveryState,
    OpenQuestion,
    PackageDelivery,
    PackageEntry,
    build_package,
)
from backoffice.learning import day_month
from backoffice.learning.plain import count_phrase
from backoffice.policy import ActionContext, ActionKind, authorize

if TYPE_CHECKING:  # the orchestrator imports this module
    from backoffice.orchestrator import Orchestrator, OutgoingMessage, Repository

__all__ = ["MAX_ATTACHMENT_BYTES", "WORKING_DAY", "PackageAgent", "PackageRecord", "portuguese_holidays",
           "working_day"]

WORKING_DAY = 3  # the report settings' default: working day 3 after month-end
# An email attachment larger than this is not sent: many mail servers refuse messages above 10-25 MB, and
# base64 adds a third. Above it the package goes without the originals (the ledger, index and manifest
# still list every one with its hash) and the email says where to download them.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


# --------------------------------------------------------------------------- working days (Portugal)


def _easter(year: int) -> date:
    """Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


@lru_cache(maxsize=64)
def portuguese_holidays(year: int) -> frozenset[date]:
    """Portugal's national public holidays (Código do Trabalho, art. 234): the fixed ones, Good Friday,
    Easter Sunday and Corpus Christi."""
    easter = _easter(year)
    fixed = [(1, 1), (4, 25), (5, 1), (6, 10), (8, 15), (10, 5), (11, 1), (12, 1), (12, 8), (12, 25)]
    return frozenset({date(year, m, d) for m, d in fixed} | {easter - timedelta(days=2), easter,
                                                              easter + timedelta(days=60)})


def working_day(year: int, month: int, n: int) -> date:
    """The ``n``-th working day of a month: Monday to Friday, public holidays in Portugal excluded."""
    if n < 1:
        raise ValueError("working days are counted from 1")
    day, seen = date(year, month, 1), 0
    holidays = portuguese_holidays(year)
    while True:
        if day.weekday() < 5 and day not in holidays:
            seen += 1
            if seen == n:
                return day
        day += timedelta(days=1)


# --------------------------------------------------------------------------- what was sent


@dataclass
class PackageRecord:
    """One month's package for one company, and where its delivery stands (§27 Day 0, Day +1).

    ``delivery`` moves PREPARED -> DELIVERED (a transport accepted the email) -> CONFIRMED (the accountant
    replied in its thread). ``number`` 2 is the final package of a month first sent with items open.
    """

    id: str
    company_id: str
    month: Month
    number: int
    complete: bool
    still_open: tuple[str, ...]
    to: str
    cc: tuple[str, ...]
    outbox_id: str
    message_id: str  # the email's Message-ID, without angle brackets: replies name it
    filename: str
    sha256: str
    size: int
    originals: bool  # the original documents are inside the attachment
    items: int
    delivery: PackageDelivery
    written_at: datetime

    @property
    def delivered(self) -> bool:
        return self.delivery.state is not DeliveryState.PREPARED

    @property
    def confirmed(self) -> bool:
        return self.delivery.confirmed


class _RegistryOriginals:
    """The package's evidence reader: the tenant's own stored originals, re-hashed by build_package."""

    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def read(self, evidence: Any) -> bytes | None:
        if not evidence.storage_key:
            return None
        try:
            return self.repo.registry.open(self.repo.tenant_id, evidence.id)
        except Exception:  # a bank line or an answer has no file worth shipping; a missing one is listed only
            return None


class PackageAgent:
    """Builds and sends each company's monthly package; follows its delivery and the accountant's reply."""

    name = "package"

    def __init__(self, orchestrator: Orchestrator) -> None:
        self.o = orchestrator

    @property
    def repo(self) -> Repository:
        return self.o.repo

    def log(self, action: str, **kwargs: Any) -> None:
        self.o.log(self.name, action, **kwargs)

    # ----------------------------------------------------------------- settings

    def settings(self) -> dict[str, Any]:
        """The owner's report settings (service.report_settings), or their defaults."""
        saved = self.repo.package_settings or {}
        return {"day": int(saved.get("day") or WORKING_DAY), "format": saved.get("format") or "zip",
                "includeDocuments": bool(saved.get("includeDocuments", True)),
                "companies": list(saved["companies"]) if "companies" in saved else list(self.repo.companies),
                "whenOpen": saved.get("whenOpen") or "send", "copyOwner": bool(saved.get("copyOwner", True)),
                "recipients": list(saved.get("recipients") or ())}

    def due_on(self, month: Month) -> date:
        """The day ``month`` goes to the accountant: the chosen working day of the month after it."""
        after = month.next()
        return working_day(after.year, after.month, self.settings()["day"])

    def recipients(self, company_id: str) -> tuple[str, tuple[str, ...]] | None:
        """(to, copies): that company's accountant first; others the owner listed for it; the owner's copy."""
        repo = self.repo
        cfg = self.settings()
        listed = [r["email"] for r in cfg["recipients"] if isinstance(r, Mapping) and r.get("email")
                  and (not isinstance(r.get("companies"), list) or company_id in r["companies"])]
        accountant = repo.accountant_for(company_id)
        people = [accountant.email.lower()] if accountant is not None else []
        people += [e.lower() for e in listed if e.lower() not in people]
        if not people:
            return None
        copies = people[1:]
        if cfg["copyOwner"] and repo.owner.email and repo.owner.email.lower() not in people:
            copies.append(repo.owner.email.lower())
        return people[0], tuple(copies)

    # ----------------------------------------------------------------- Day 0: build and write

    def records(self, company_id: str, month: Month) -> list[PackageRecord]:
        return sorted((p for p in self.repo.packages.values() if p.company_id == company_id and p.month == month),
                      key=lambda p: p.number)

    def deliver_due(self, now: datetime) -> list[str]:
        """On (or after) the chosen working day, write each company's package for the month before (§27).

        A month with items open goes with an honest note, or waits until it closes (``whenOpen``); a month
        sent incomplete gets its final package once it closes. Returns the packages written now.
        """
        from backoffice.orchestrator import TZ

        repo = self.repo
        today = now.astimezone(TZ).date()
        month = Month.of(today).previous()
        if today < self.due_on(month):
            return []
        cfg = self.settings()
        written: list[str] = []
        for company_id in sorted(repo.companies):
            if company_id not in cfg["companies"]:
                continue
            done = self.records(company_id, month)
            closed = (company_id, str(month)) in repo.closed_months
            if done and (done[-1].complete or not closed):
                continue  # sent; after an incomplete one, the final package follows only once the month closes
            if not done and not closed and cfg["whenOpen"] == "wait":
                continue
            people = self.recipients(company_id)
            if people is None:
                continue  # no accountant named: nobody to send it to (the settings say so)
            decision = authorize(ActionKind.DOCUMENT_DELIVERY, repo.policy, ActionContext(
                tenant_id=repo.tenant_id, entity_id=company_id, subject_id=f"package:{company_id}:{month}"))
            if not decision.allowed_now:
                continue
            items = repo.items_for(company_id, month)
            if not items:
                continue  # nothing happened in that month for this company: nothing to send
            record = self._write(company_id, month, items, people, now, closed=closed, number=len(done) + 1)
            written.append(record.id)
        return written

    def entries(self, company_id: str, month: Month, items: Sequence[Any]) -> list[PackageEntry]:
        """Each of the month's items as the accountant books it: the payment, the documents that prove it,
        the evidence and the "Why?" (§54). Supporting documents are their own entries, marked as such."""
        repo = self.repo
        out: list[PackageEntry] = []
        for item in items:
            if item.subject_type == "transaction":
                rec = repo.transactions[item.subject_id]
                records = [repo.documents[d] for d in rec.document_ids if d in repo.documents]
                why: Sequence[str] = [w.replace(": ", " ", 1) for w in rec.match_why] or (
                    [rec.decision.reason] if rec.decision is not None else [])
                ids = [rec.evidence_id, *rec.proof_evidence_ids, *(e for d in records for e in d.evidence_ids)]
                kwargs: dict[str, Any] = {"transaction": rec.tx, "documents": [d.document for d in records],
                                          "why": why}
            elif item.subject_type == "document":
                record = repo.documents[item.subject_id]
                ids = list(record.evidence_ids)
                kwargs = {"documents": [record.document],
                          "booked_on": record.document.issue_date or record.received_at.date(),
                          "why": [record.hold_reason] if record.hold_reason else []}
            else:
                continue
            evidence = []
            for evidence_id in dict.fromkeys(ids):
                try:
                    evidence.append(repo.evidence(evidence_id))
                except Exception:  # an id without a stored original is left out, never invented
                    continue
            entry = PackageEntry.from_domain(item, evidence=evidence, **kwargs)
            out.append(replace(entry, entity_id=company_id))
        return out

    def _open_lines(self, company_id: str, month: Month) -> tuple[str, ...]:
        """What is still open in the month, in the owner's plain words (the package's honest note)."""
        status = self.o.month_status(company_id, month)
        return tuple(status.reasons()) or ("Some items are not settled yet.",)

    def _questions(self, company_id: str) -> list[OpenQuestion]:
        return [OpenQuestion(question_id=q.id, text=q.text, asked_at=q.asked_at)
                for q in sorted(self.repo.accountant_questions.values(), key=lambda q: q.id)
                if q.company_id == company_id and q.status != "answered"]

    def _build(self, company_id: str, month: Month, items: Sequence[Any], now: datetime,
               still_open: Sequence[str]) -> tuple[AccountantPackage, bool]:
        """The package, with the originals when they fit in an email (§52: re-hashed on the way in)."""
        entity = self.repo.companies[company_id]
        entries = self.entries(company_id, month, items)
        cfg = self.settings()
        kwargs: dict[str, Any] = {"generated_at": now, "questions": self._questions(company_id),
                                  "still_open": still_open}
        wanted = cfg["includeDocuments"] and cfg["format"] == "zip"
        if wanted:
            package = build_package(entity, month, entries, evidence_reader=_RegistryOriginals(self.repo), **kwargs)
            if len(package.data) <= MAX_ATTACHMENT_BYTES:
                return package, True
        return build_package(entity, month, entries, **kwargs), False

    def _write(self, company_id: str, month: Month, items: Sequence[Any], people: tuple[str, tuple[str, ...]],
               now: datetime, *, closed: bool, number: int) -> PackageRecord:
        from backoffice.missing import thread_token
        from backoffice.orchestrator import MESSAGE_ID_DOMAIN, TZ

        repo = self.repo
        still_open = () if closed else self._open_lines(company_id, month)
        package, originals = self._build(company_id, month, items, now, still_open)
        cfg = self.settings()
        if cfg["format"] == "csv":
            with zipfile.ZipFile(io.BytesIO(package.data)) as archive:
                ledger = archive.read("ledger.csv")
            files = [(package.filename.replace(".zip", "-ledger.csv"), "text/csv", ledger)]
        else:
            files = [(package.filename, "application/zip", package.data)]
        to, cc = people
        token = thread_token(repo.tenant_id, f"package:{company_id}:{month}:{number}").lower()
        message_id = f"package-{token}-{month.year:04d}{month.month:02d}-{number}@{MESSAGE_ID_DOMAIN}"
        legal = repo.legal_names.get(company_id) or repo.company_name(company_id) or "the company"
        accountant = repo.accountant_for(company_id)
        first = accountant.person.split()[0] if accountant is not None and accountant.person.strip() and \
            accountant.email.lower() == to else ""
        period = f"{month.name} {month.year}"
        subject = f"{legal}: {period} accounts" + (" (final)" if number > 1 else "")
        body = self._body(legal, period, month, items, still_open, originals, cfg, number, first,
                          size=len(package.data)) + f"{repo.owner.full_name}\n"
        out = self.o.write_email("accountant_package", f"pkg_{company_id}_{month}_{number}", company_id, to,
                                 subject, body, now, headers=(("Message-ID", f"<{message_id}>"),), files=files,
                                 cc=cc)
        record = PackageRecord(
            id=f"pkg_{company_id}_{month}_{number}", company_id=company_id, month=month, number=number,
            complete=closed, still_open=tuple(still_open), to=to, cc=cc, outbox_id=out.id, message_id=message_id,
            filename=files[0][0], sha256=package.sha256, size=len(files[0][2]), originals=originals,
            items=len(items), delivery=package.delivery(), written_at=now)
        repo.packages[record.id] = record
        self.log("package_written", subject_id=record.id,
                 values={"company": company_id, "month": str(month), "to": to, "copies": list(cc),
                         "outbox_id": out.id, "sha256": package.sha256, "size": record.size,
                         "originals": originals, "complete": closed, "still_open": list(still_open)},
                 response={"due_on": self.due_on(month).isoformat(),
                           "written_on": now.astimezone(TZ).date().isoformat()})
        return record

    @staticmethod
    def _body(legal: str, period: str, month: Month, items: Sequence[Any], still_open: Sequence[str],
              originals: bool, cfg: Mapping[str, Any], number: int, first: str, *, size: int) -> str:
        payments = sum(1 for i in items if i.subject_type == "transaction")
        documents = sum(1 for i in items if i.subject_type == "document")
        what = (f"{count_phrase(payments, 'payment')} and {count_phrase(documents, 'document')}")
        lines = [f"Hello{' ' + first if first else ''},", ""]
        if number > 1:
            lines.append(f"{period} for {legal} is now complete. Here is the final package, replacing the one I "
                         "sent before.")
        else:
            lines.append(f"Here is {period} for {legal}: {what}.")
        if cfg["format"] == "csv":
            lines.append("Attached: the ledger (CSV).")
        else:
            lines.append("Attached: the ledger (ledger.csv), the evidence index (evidence_index.csv), the manifest "
                         "with every file's hash" + (" and the original documents." if originals else "."))
            lines.append("Supporting documents (pro-formas, quotes, delivery notes) are marked as supporting in the "
                         "ledger and are not booked.")
        if cfg["format"] == "zip" and cfg["includeDocuments"] and not originals:
            mb = max(1, round(size / (1024 * 1024)))
            lines.append(f"The original documents are too large to attach here (over {mb} MB). You can download "
                         "them from your accountant workspace.")
        lines.append("")
        if still_open:
            lines.append(f"Not everything in {month.name} is settled yet. Still open:")
            lines += [f"- {line}" for line in still_open]
            lines.append("I will send the final package once these are settled.")
        else:
            lines.append(f"Everything in {month.name} is settled: every payment has its evidence.")
        lines += ["", "Please reply to this email to confirm you received it.", "", "Kind regards,"]
        return "\n".join(lines) + "\n"

    # ----------------------------------------------------------------- Day +1: delivered, confirmed

    def by_outbox(self, outbox_id: str) -> PackageRecord | None:
        return next((p for p in self.repo.packages.values() if p.outbox_id == outbox_id), None)

    def sent(self, message: OutgoingMessage, at: datetime) -> None:
        """A transport accepted the package's email: only now is the month delivered (§27 Day +1)."""
        record = self.by_outbox(message.id)
        if record is None:
            return
        try:
            record.delivery = record.delivery.deliver(at=at, channel=DeliveryChannel.EMAIL, recipient=record.to,
                                                      evidence_id=message.id)
        except DeliveryError:
            return
        self.log("package_delivered", subject_id=record.id,
                 values={"to": record.to, "copies": list(record.cc), "sha256": record.sha256})
        self.o.activity(at, "closed", f"{record.month.name} sent to your accountant." if record.number == 1 else
                        f"The final {record.month.name} package sent to your accountant.", record.company_id,
                        tag="package")

    def acknowledged(self, parsed: Any, evidence_id: str, at: datetime) -> PackageRecord | None:
        """The accountant's reply in the package's thread confirms they have it (Day +1). Matched by the thread
        only (In-Reply-To / References), from the address it went to or that address's own domain."""
        refs = {r.strip("<>").lower() for r in (*parsed.thread.in_reply_to, *parsed.thread.references)}
        if not refs:
            return None
        record = next((p for p in sorted(self.repo.packages.values(), key=lambda p: p.id)
                       if p.message_id.lower() in refs), None)
        if record is None or record.confirmed or not record.delivered:
            return None
        sender = (parsed.sender.address if parsed.sender else "").strip().lower()
        allowed = {record.to, *record.cc}
        domain = record.to.rsplit("@", 1)[-1]
        if sender not in allowed and sender.rsplit("@", 1)[-1] != domain:
            self.log("package_reply_refused", subject_id=record.id, evidence_ids=[evidence_id],
                     response={"reason": "not from the accountant's address"})
            return None
        try:
            record.delivery = record.delivery.confirm(at=at, evidence_id=evidence_id, by=sender)
        except DeliveryError:
            return None
        self.log("package_confirmed", subject_id=record.id, evidence_ids=[evidence_id], values={"by": sender})
        self.o.activity(at, "closed", f"Your accountant confirmed they have {record.month.name}.", record.company_id,
                        evidence_ids=[evidence_id], tag="package")
        return record

    # ----------------------------------------------------------------- what the owner reads

    def owner_view(self, company_id: str, month: Month, today: date | None = None) -> dict[str, Any] | None:
        """Where the month's package stands, in plain words: never "sent" before a transport accepted it."""
        records = self.records(company_id, month)
        if not records:
            return None  # nothing written yet: nothing is claimed
        last = records[-1]
        sent = [r for r in records if r.delivered]
        message = self.repo.outbox.get(last.outbox_id)
        out: dict[str, Any] = {"dueOn": self.due_on(month).isoformat(), "complete": last.complete,
                               "to": last.to, "filename": last.filename, "originals": last.originals}
        if not sent:
            if message is not None and self.o.held_back(message):
                out.update(state="held", text=f"{month.name} is ready for your accountant, but sending it is "
                                              "switched off, so I have not sent it.")
            else:
                out.update(state="waiting", text=f"{month.name} is ready for your accountant. It is waiting to be "
                                                 "sent.")
            return out
        from backoffice.orchestrator import TZ

        latest = sent[-1]
        assert latest.delivery.delivered_at is not None
        on = latest.delivery.delivered_at.astimezone(TZ).date()
        if latest.number > 1:
            text = f"The final {month.name} package went to your accountant on {day_month(on, today)}."
        else:
            text = f"{month.name} sent to your accountant on {day_month(on, today)}."
        if not latest.complete:
            text += f" It went with a note of what is still open ({count_phrase(len(latest.still_open), 'thing')})."
        confirmed = latest.delivery.confirmed_at
        if confirmed is not None:
            text += " They confirmed they have it."
        out.update(state="confirmed" if confirmed is not None else "sent", text=text, sentOn=on.isoformat(),
                   confirmedOn=confirmed.astimezone(TZ).date().isoformat() if confirmed is not None else None,
                   stillOpen=list(latest.still_open))
        return out
