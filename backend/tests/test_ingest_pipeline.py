"""End to end: mailbox sync → email evidence → invoice link → downloaded original (§3, §8, §9)."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path

import httpx

from backoffice.connectors import ConnectorKind, ConnectorState, GmailConnector, MailItem, covers
from backoffice.domain.models import EvidenceFormat
from backoffice.evidence import (
    EmailIngestor,
    EvidenceRegistry,
    LinkFetcher,
    LinkOutcome,
    LocalObjectStore,
    UrlSafety,
    UrlSafetyConfig,
    register_fetch,
)

NOW = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)
EML = (Path(__file__).parent / "fixtures" / "email" / "link_only_invoice.eml").read_bytes()
PDF = b"%PDF-1.4\n% INV-5521\n%%EOF\n"


class Tokens:
    def access_token(self):
        return "at"

    def invalidate(self):
        pass


def gmail_api(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/profile"):
        return httpx.Response(200, json={"historyId": "500"})
    if path.endswith("/messages"):
        return httpx.Response(200, json={"messages": [{"id": "m1"}]})
    if path.endswith("/messages/m1"):
        raw = base64.urlsafe_b64encode(EML).decode().rstrip("=")
        return httpx.Response(200, json={"id": "m1", "threadId": "t1", "internalDate": "1758792600000", "raw": raw})
    return httpx.Response(404)


def supplier_site(request: httpx.Request) -> httpx.Response:
    if request.headers["host"] == "billing.acme-cloud.com" and request.url.path == "/i/5521":
        return httpx.Response(302, headers={"location": "https://files.acme-cloud.com/5521.pdf"})
    if request.headers["host"] == "files.acme-cloud.com":
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
    return httpx.Response(404)


class Resolver:
    def resolve(self, host, port):
        return {"billing.acme-cloud.com": ["93.184.216.36"], "files.acme-cloud.com": ["93.184.216.37"]}.get(host, [])


def test_invoice_behind_a_button_ends_up_as_evidence_with_provenance(tmp_path):
    registry = EvidenceRegistry(LocalObjectStore(tmp_path), clock=lambda: NOW)
    ingestor = EmailIngestor(registry)
    fetcher = LinkFetcher(UrlSafety(UrlSafetyConfig(known_domains=("acme-cloud.com",)), Resolver()),
                          transport=httpx.MockTransport(supplier_site), clock=lambda: NOW)
    gmail = GmailConnector(Tokens(), client=httpx.Client(transport=httpx.MockTransport(gmail_api)), clock=lambda: NOW)
    state = ConnectorState(tenant_id="t1", kind=ConnectorKind.GMAIL, account="ana@padaria.pt")

    downloaded = []

    def sink(item: MailItem) -> None:
        result = ingestor.ingest(item.raw, tenant_id="t1", received_at=item.received_at,
                                 context={"connector_id": state.connector_id, "provider_id": item.provider_id})
        for link in result.parsed.invoice_links[:1]:
            fetch = fetcher.fetch(link.url, supplier_name="Acme Cloud")
            assert fetch.outcome is LinkOutcome.DOWNLOADED
            downloaded.extend(register_fetch(fetch, registry, tenant_id="t1",
                                             context={"message_evidence_id": result.message.evidence.id}))

    outcome = gmail.sync(state, sink)
    assert outcome.ok and outcome.delivered == 1
    (pdf,) = downloaded
    ev = pdf.evidence
    assert ev.format is EvidenceFormat.PDF and registry.open("t1", ev.id) == PDF
    assert ev.original_url == "https://billing.acme-cloud.com/i/5521"
    assert ev.metadata["fetch"]["final_url"] == "https://files.acme-cloud.com/5521.pdf"
    # The mailbox is now covered up to the sync; the current month is not closed yet.
    assert not covers(outcome.state, NOW.date().replace(day=1), NOW.date())
