"""Where each supplier's invoices can be fetched: its website, learned (§9, §10, §22, checklist L6).

The owner connects a supplier's website once (Sources: the supplier, a username, a password for the vault). The
engine then learns which supplier's invoices that website holds, and remembers it per supplier
(``repo.supplier_websites``):

* **fetched there**: the daily sign-in brought an invoice of that supplier (``portal.retrieved``), or a search for
  a missing invoice found it there (``search.recorded``) -> ``retrieved`` / ``found``;
* **an email showed it**: an invoice email from the supplier with nothing attached, only a "view your invoice"
  link to a known supplier website (backoffice.invoice_sites) or to a connected one -> ``email_link`` (the host
  is remembered even before the owner connects that website);
* **the owner said so**: the website was added under the supplier's name -> ``named``.

Everything here runs when an event is applied (live and on replay alike), from what the event carries: nothing is
fetched, and each change is written to the audit trail, so a replay rebuilds the same knowledge.

It is used where it matters: the owner reads it on the supplier ("Invoices: fetched from edp.pt"), and when an
invoice from that supplier is missing, its website is searched before anyone is asked (backoffice.evidence_search):
first of all places once there is evidence its invoices are there (fetched, or an email's link), else in the spec's
order with the other connected places (§22 search 4).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from backoffice.invoice_sites import host_of, on_host, site_for_host
from backoffice.learning.keys import counterparty_key, display_name
from backoffice.orchestrator import ConnectorState, _Agent

__all__ = ["EVIDENCE", "LEARNED_BY", "SupplierWebsite", "WebsiteAgent"]

LEARNED_BY = ("named", "email_link", "retrieved", "found")  # weakest first
EVIDENCE = frozenset({"email_link", "retrieved", "found"})  # its invoices were seen there: asked first
FETCHED = frozenset({"retrieved", "found"})


@dataclass(frozen=True)
class SupplierWebsite:
    """What is known about where one supplier's invoices are."""

    supplier_id: str
    how: str  # one of LEARNED_BY
    at: datetime
    host: str | None = None  # "edp.pt": what the owner reads
    connection_id: str | None = None  # the owner's sign-in there, when connected
    evidence_ids: tuple[str, ...] = ()  # what taught it (the invoice fetched, the email with the link)


def _keys(names: Iterable[str | None]) -> set[str]:
    out: set[str] = set()
    for name in names:
        for n in (name, display_name(name, fallback="") if name else None):
            key = counterparty_key(n)
            if key:
                out.add(key)
    return out


def _same_name(a: set[str], b: set[str]) -> bool:
    """One name is the other, or starts it as whole words ("edp" and "edp comercial")."""
    return any(x == y or x.startswith(y + " ") or y.startswith(x + " ") for x in a for y in b)


class WebsiteAgent(_Agent):
    """Learns and answers where each supplier's invoices are (module docstring)."""

    name = "retrieval"

    # ----------------------------------------------------------------- the business's supplier websites

    def _websites(self) -> list[ConnectorState]:
        return sorted((c for c in self.repo.connectors.values() if c.kind == "portal"), key=lambda c: c.id)

    @staticmethod
    def _supplier_names(supplier: Any) -> set[str]:
        return _keys([supplier.name, *supplier.aliases])

    def _supplier_domains(self, supplier: Any) -> list[str]:
        domains = [d.lower() for d in supplier.email_domains]
        if supplier.contact_email and "@" in supplier.contact_email:
            domains.append(supplier.contact_email.split("@", 1)[1].lower())
        return domains

    def is_for(self, c: ConnectorState, supplier: Any) -> bool:
        """The website was added under this supplier's name, or is on its email's domain."""
        if _same_name(_keys([c.name]), self._supplier_names(supplier)):
            return True
        return bool(c.hosts) and any(on_host(d, c.hosts) for d in self._supplier_domains(supplier))

    def connections_for(self, supplier: Any, company_id: str | None = None, *,
                        healthy: bool = True) -> list[tuple[ConnectorState, bool]]:
        """The connected websites holding this supplier's invoices, each with whether it is asked first (there is
        evidence its invoices are there); those first. ``company_id``: only websites that serve that company."""
        if supplier is None:
            return []
        record: SupplierWebsite | None = self.repo.supplier_websites.get(supplier.id)
        out: list[tuple[ConnectorState, bool]] = []
        for c in self._websites():
            if healthy and not c.healthy:
                continue
            if company_id is not None and c.company_ids and company_id not in c.company_ids:
                continue
            learned = record is not None and (
                record.connection_id == c.id
                or (record.connection_id not in self.repo.connectors and on_host(record.host, c.hosts) is not None))
            if learned or self.is_for(c, supplier):
                out.append((c, learned and record is not None and record.how in EVIDENCE))
        out.sort(key=lambda pair: (not pair[1], pair[0].id))
        return out

    # ----------------------------------------------------------------- learning

    def learn(self, supplier_id: str | None, how: str, at: datetime, *, connection_id: str | None = None,
              host: str | None = None, evidence_ids: Sequence[str] = ()) -> SupplierWebsite | None:
        """Remember (or strengthen) where ``supplier_id``'s invoices are. Weaker news never overrides stronger;
        an invoice fetched from another website moves it there. Each change is audited. Returns the record."""
        repo = self.repo
        if not supplier_id or supplier_id not in repo.suppliers or how not in LEARNED_BY:
            return None
        c = repo.connectors.get(connection_id or "")
        if c is None or c.kind != "portal":
            c, connection_id = None, None
        host = host or (c.hosts[0] if c is not None and c.hosts else None)
        old: SupplierWebsite | None = repo.supplier_websites.get(supplier_id)
        if old is None:
            new = SupplierWebsite(supplier_id, how, at, host, connection_id, tuple(evidence_ids))
        else:
            rank = LEARNED_BY.index
            stronger = rank(how) >= rank(old.how)
            moves = connection_id is not None and connection_id != old.connection_id and (
                old.connection_id not in repo.connectors or how in FETCHED or stronger)
            new = replace(
                old, how=how if stronger else old.how,
                connection_id=connection_id if moves else old.connection_id,
                host=(host or old.host) if moves or old.host is None or (stronger and host) else old.host)
            if (new.how, new.connection_id, new.host) == (old.how, old.connection_id, old.host):
                return old  # nothing new: no audit line for the daily sign-in that fetched it again
            new = replace(new, at=at, evidence_ids=tuple(dict.fromkeys([*old.evidence_ids, *evidence_ids]))[-5:])
        repo.supplier_websites[supplier_id] = new
        self.log("website_learned", subject_id=supplier_id, evidence_ids=list(evidence_ids),
                 values={"how": new.how, "host": new.host or "", "connection": new.connection_id or ""})
        return new

    def connected(self, connection_id: str, at: datetime) -> None:
        """The owner added a supplier's website: it holds the invoices of the supplier it was named for, and of a
        supplier an email already showed keeps them on its host."""
        c = self.repo.connectors.get(connection_id)
        if c is None or c.kind != "portal":
            return
        for supplier in sorted(self.repo.suppliers.values(), key=lambda s: s.id):
            record = self.repo.supplier_websites.get(supplier.id)
            if record is not None and record.connection_id is None and on_host(record.host, c.hosts):
                self.learn(supplier.id, record.how, at, connection_id=c.id)
            elif _same_name(_keys([c.name]), self._supplier_names(supplier)):
                self.learn(supplier.id, "named", at, connection_id=c.id)

    def fetched(self, connection_id: str, evidence_ids: Sequence[str], at: datetime, *, how: str = "retrieved") -> None:
        """Invoices fetched from a supplier's website: each of their suppliers keeps its invoices there."""
        wanted = set(evidence_ids)
        if not wanted:
            return
        by_supplier: dict[str, list[str]] = {}
        for d in sorted(self.repo.documents.values(), key=lambda d: d.id):
            if d.supplier_id and not d.sales and wanted & set(d.evidence_ids):
                by_supplier.setdefault(d.supplier_id, []).extend(e for e in d.evidence_ids if e in wanted)
        for supplier_id, evidence in sorted(by_supplier.items()):
            self.learn(supplier_id, how, at, connection_id=connection_id, evidence_ids=evidence[:1])

    def from_email(self, supplier: Any, links: Sequence[str], document_ids: Sequence[str], at: datetime, *,
                   evidence_id: str | None = None) -> None:
        """An invoice email with nothing attached, only "view your invoice" links: when one goes to a known supplier
        website (or a connected one) that is this supplier's, its host is where the supplier's invoices are."""
        repo = self.repo
        if supplier is None:  # the sender is not a known supplier: the one its link's invoice named, if only one
            named = {repo.documents[d].supplier_id for d in document_ids if d in repo.documents}
            named.discard(None)
            supplier = repo.suppliers.get(named.pop()) if len(named) == 1 else None
        if supplier is None:
            return
        for url in links:
            host = host_of(url)
            site = site_for_host(host)
            if site is not None and (_same_name(_keys(site.names), self._supplier_names(supplier))
                                     or any(on_host(d, site.hosts) for d in self._supplier_domains(supplier))):
                connection = next((c for c in self._websites() if on_host(host, c.hosts)), None)
                self.learn(supplier.id, "email_link", at, host=on_host(host, site.hosts),
                           connection_id=connection.id if connection is not None else None,
                           evidence_ids=[evidence_id] if evidence_id else ())
                return
            connection = next((c for c in self._websites() if on_host(host, c.hosts) and self.is_for(c, supplier)),
                              None)
            if connection is not None:
                self.learn(supplier.id, "email_link", at, host=on_host(host, connection.hosts),
                           connection_id=connection.id, evidence_ids=[evidence_id] if evidence_id else ())
                return

    # ----------------------------------------------------------------- what the owner reads

    def words(self, supplier: Any) -> str | None:
        """"Invoices: fetched from edp.pt" once fetched there; "Invoices: on edp.pt" when only known; else None."""
        if supplier is None:
            return None
        record: SupplierWebsite | None = self.repo.supplier_websites.get(supplier.id)
        connections = [c for c, _ in self.connections_for(supplier, healthy=False)]
        host = (record.host if record is not None else None) or next((c.hosts[0] for c in connections if c.hosts),
                                                                     None)
        where = host or (f"{connections[0].name}'s website" if connections else None)
        if where is None:
            return None
        fetched = record is not None and record.how in FETCHED
        return f"Invoices: fetched from {where}" if fetched else f"Invoices: on {where}"

    def view(self, supplier: Any) -> dict[str, Any] | None:
        """Where a supplier's invoices are, for the owner's supplier list and the accountant."""
        words = self.words(supplier)
        if words is None:
            return None
        record: SupplierWebsite | None = self.repo.supplier_websites.get(supplier.id)
        connections = self.connections_for(supplier, healthy=False)
        return {"text": words, "host": record.host if record is not None else None,
                "connectionId": connections[0][0].id if connections else None,
                "fetched": record is not None and record.how in FETCHED,
                "searchedFirst": any(first for _, first in connections)}
