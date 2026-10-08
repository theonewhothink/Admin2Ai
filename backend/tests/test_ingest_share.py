"""Mobile share-extension intake (§12)."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from backoffice.domain.models import EvidenceFormat, SourceKind
from backoffice.evidence.links import LinkFetcher, LinkOutcome, UrlSafety, UrlSafetyConfig
from backoffice.evidence.share import GOT_IT, ShareIntake, ShareKind, SharePayload, ShareRoute, route_for
from backoffice.evidence.store import EvidenceRegistry, LocalObjectStore

T0 = datetime(2026, 9, 24, 9, 0, tzinfo=timezone.utc)
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\n%%EOF\n"
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
EML = (Path(__file__).parent / "fixtures" / "email" / "vodafone_invoice.eml").read_bytes()


class Resolver:
    def resolve(self, host, port):
        return {"minha.vodafone.pt": ["93.184.216.35"], "wa.example": ["93.184.216.40"]}.get(host, [])


def portal(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/f/1.pdf":
        return httpx.Response(200, content=PDF, headers={"content-type": "application/pdf"})
    if request.url.path == "/mfa":
        return httpx.Response(200, html='<form action="/v"><input autocomplete="one-time-code"></form>')
    return httpx.Response(404)


@pytest.fixture
def registry(tmp_path) -> EvidenceRegistry:
    return EvidenceRegistry(LocalObjectStore(tmp_path), clock=lambda: T0)


@pytest.fixture
def intake(registry) -> ShareIntake:
    fetcher = LinkFetcher(UrlSafety(UrlSafetyConfig(known_domains=("vodafone.pt",)), Resolver()),
                          transport=httpx.MockTransport(portal), clock=lambda: T0)
    return ShareIntake(registry, fetcher=fetcher, clock=lambda: T0)


# --------------------------------------------------------------------------- routing


@pytest.mark.parametrize(
    ("payload", "route"),
    [
        (SharePayload.for_url("https://minha.vodafone.pt/f/1.pdf"), ShareRoute.LINK),
        (SharePayload.for_text("  https://minha.vodafone.pt/f/1.pdf \n"), ShareRoute.LINK),
        (SharePayload.for_text("Paid Vodafone 92,40 today"), ShareRoute.TEXT),
        (SharePayload.for_file(PDF, "fatura.pdf"), ShareRoute.DOCUMENT),
        (SharePayload.for_file(PNG, "IMG_2231.PNG"), ShareRoute.IMAGE),
        (SharePayload.for_file(PNG, "Screenshot 2026-09-24 at 10.12.png"), ShareRoute.SCREENSHOT),
        (SharePayload.for_file(PNG, "Captura de ecrã 2026-09-24.png"), ShareRoute.SCREENSHOT),
        (SharePayload.for_file(PNG, "photo.png", is_screenshot=True), ShareRoute.SCREENSHOT),
        (SharePayload.for_file(EML, "fatura.eml", "message/rfc822"), ShareRoute.EMAIL),
        (SharePayload.for_file(b'<?xml version="1.0"?><r/>', "f.xml"), ShareRoute.DOCUMENT),
        (SharePayload.for_file(bytes(range(256)) * 4, "x.bin"), ShareRoute.UNSUPPORTED),
        (SharePayload(ShareKind.FILE), ShareRoute.UNSUPPORTED),
        (SharePayload.for_text("   "), ShareRoute.UNSUPPORTED),
    ],
)
def test_route_for(payload, route):
    assert route_for(payload) is route


# --------------------------------------------------------------------------- intake


def test_shared_link_is_recorded_and_followed(intake, registry):
    outcome = intake.accept("t1", SharePayload.for_url("https://minha.vodafone.pt/f/1.pdf",
                                                       source_app="net.whatsapp.WhatsApp"))
    assert outcome.route is ShareRoute.LINK and outcome.owner_message == GOT_IT and outcome.accepted
    url_ev, pdf_ev = (r.evidence for r in outcome.registrations)
    assert url_ev.format is EvidenceFormat.URL and url_ev.original_url == "https://minha.vodafone.pt/f/1.pdf"
    assert url_ev.source_kind is SourceKind.MOBILE_SHARE
    assert pdf_ev.format is EvidenceFormat.PDF and pdf_ev.original_url == "https://minha.vodafone.pt/f/1.pdf"
    sighting = registry.sightings("t1", pdf_ev.id)[0]
    assert sighting.context["shared_url_evidence_id"] == url_ev.id
    assert sighting.context["source_app"] == "net.whatsapp.WhatsApp"


def test_shared_link_needing_a_code_tells_the_owner(intake):
    outcome = intake.accept("t1", SharePayload.for_url("https://minha.vodafone.pt/mfa"))
    assert outcome.fetches[0].outcome is LinkOutcome.MFA_REQUIRED
    assert outcome.owner_message == "Vodafone needs authentication."
    assert len(outcome.registrations) == 1  # the link itself is still evidence


def test_unsafe_shared_link_is_kept_but_never_opened(intake):
    outcome = intake.accept("t1", SharePayload.for_url("http://169.254.169.254/latest"))
    assert outcome.fetches[0].outcome is LinkOutcome.BLOCKED_UNSAFE
    assert outcome.owner_message == "This link didn't look safe, so I didn't open it."
    assert [r.evidence.format for r in outcome.registrations] == [EvidenceFormat.URL]


def test_text_with_links_is_stored_and_links_followed(intake):
    text = "Olá! A fatura está aqui: https://minha.vodafone.pt/f/1.pdf obrigado"
    outcome = intake.accept("t1", SharePayload.for_text(text))
    assert outcome.route is ShareRoute.TEXT
    formats = [r.evidence.format for r in outcome.registrations]
    assert formats == [EvidenceFormat.TEXT, EvidenceFormat.PDF]


def test_without_a_fetcher_links_wait(registry):
    outcome = ShareIntake(registry, clock=lambda: T0).accept("t1", SharePayload.for_url("https://minha.vodafone.pt/f/1.pdf"))
    assert outcome.pending_links == ("https://minha.vodafone.pt/f/1.pdf",)
    assert outcome.owner_message == GOT_IT


def test_screenshot_and_photo_formats(intake):
    shot = intake.accept("t1", SharePayload.for_file(PNG, "photo.png", is_screenshot=True))
    assert shot.route is ShareRoute.SCREENSHOT
    assert shot.registrations[0].evidence.format is EvidenceFormat.SCREENSHOT
    photo = intake.accept("t1", SharePayload.for_file(PNG + b"\0", "IMG_1.png"))
    assert photo.registrations[0].evidence.format is EvidenceFormat.IMAGE


def test_email_export_goes_through_email_ingestion(intake):
    outcome = intake.accept("t1", SharePayload.for_file(EML, "Fatura.eml", "message/rfc822"))
    assert outcome.route is ShareRoute.EMAIL
    assert outcome.email.message.evidence.format is EvidenceFormat.EML
    assert outcome.email.message.evidence.source_kind is SourceKind.MOBILE_SHARE
    assert {f.filename for f in outcome.email.files} == {"Fatura FT 2026-183.pdf", "logo.png"}


def test_whatsapp_chat_export_zip_is_expanded(intake):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("_chat.txt", "[24/09/26 10:01] Ana: fatura em anexo")
        zf.writestr("00000012-DOCUMENT-fatura.pdf", PDF)
        zf.writestr("../escape.pdf", PDF)
    outcome = intake.accept("t1", SharePayload.for_file(buf.getvalue(), "WhatsApp Chat - Hazel Tree.zip"))
    assert outcome.route is ShareRoute.ARCHIVE
    formats = [r.evidence.format for r in outcome.registrations]
    assert formats == [EvidenceFormat.ZIP, EvidenceFormat.TEXT, EvidenceFormat.PDF]
    assert outcome.skipped == ("../escape.pdf:path_traversal",)


def test_unreadable_empty_and_oversized_files_get_plain_answers(registry):
    intake = ShareIntake(registry, max_bytes=100)
    assert intake.accept("t1", SharePayload.for_file(bytes(range(90)), "x.bin")).owner_message == (
        "I can't read this kind of file yet.")
    assert intake.accept("t1", SharePayload(ShareKind.FILE)).owner_message == "There was nothing to save."
    big = intake.accept("t1", SharePayload.for_file(PDF * 10, "big.pdf"))
    assert big.owner_message == "This file is too large to send." and not big.accepted
