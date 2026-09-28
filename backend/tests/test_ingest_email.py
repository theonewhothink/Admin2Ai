"""Email evidence: .eml / Gmail raw parsing, link ranking, ingestion (§7-9, §55)."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.message import EmailMessage
from email import policy
from pathlib import Path

import pytest

from backoffice.domain.models import EvidenceFormat, SourceKind
from backoffice.evidence.email import (
    EmailIngestor,
    EmailLimits,
    EmailParseError,
    decode_gmail_raw,
    extract_links,
    parse_eml,
    parse_gmail_raw,
    score_link,
)
from backoffice.evidence.store import EvidenceRegistry, LocalObjectStore

FIXTURES = Path(__file__).parent / "fixtures" / "email"
T0 = datetime(2026, 9, 24, 8, 15, tzinfo=timezone.utc)
PDF = b"%PDF-1.4\n1 0 obj<< /Type /Catalog >>endobj\ntrailer<< /Root 1 0 R >>\n%%EOF\n"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def ingestor(tmp_path) -> EmailIngestor:
    return EmailIngestor(EvidenceRegistry(LocalObjectStore(tmp_path), clock=lambda: T0))


# --------------------------------------------------------------------------- parsing


def test_supplier_invoice_email_is_fully_read():
    email = parse_eml(fixture("vodafone_invoice.eml"))
    assert email.subject == "A sua fatura Vodafone de setembro"
    assert email.sender.address == "faturas@vodafone.pt" and email.sender.name == "Vodafone"
    assert email.sender_domain == "vodafone.pt"
    assert email.date == datetime(2026, 9, 24, 8, 15, tzinfo=timezone.utc)
    assert email.thread.message_id == "fatura-2026-09-183@vodafone.pt"
    assert email.bulk  # List-Unsubscribe
    assert "fatura de setembro" in email.text_body
    assert "Ver fatura" in email.html_body
    names = {p.filename: p for p in email.attachments}
    assert names["Fatura FT 2026-183.pdf"].data.startswith(b"%PDF")
    logo = names["logo.png"]
    assert logo.content_id == "logo@vodafone.pt" and logo.inline  # referenced as cid: in the HTML
    assert email.inline_images == (logo,)


def test_invoice_links_are_ranked_and_marketing_links_are_not():
    email = parse_eml(fixture("vodafone_invoice.eml"))
    likely = [link.url for link in email.invoice_links]
    assert likely == [
        "https://minha.vodafone.pt/faturas/FT2026-183.pdf",
        "https://minha.vodafone.pt/faturas/view?id=FT2026-183&src=email",  # &amp; decoded
    ]
    button = email.invoice_links[1]
    assert button.text == "Ver fatura" and button.button_like and "sender_domain" in button.reasons
    others = {link.url: link for link in email.links if not link.invoice_likely}
    assert "https://www.facebook.com/vodafonept" in others
    assert "https://www.vodafone.pt/unsubscribe?u=1" in others
    assert all(not link.url.startswith("mailto:") for link in email.links)


def test_outlook_vml_button_and_view_in_browser():
    email = parse_eml(fixture("link_only_invoice.eml"))
    assert not email.attachments
    top = email.links[0]
    assert top.url == "https://billing.acme-cloud.com/i/5521"
    assert top.text == "View invoice" and top.button_like and top.invoice_likely
    browser = next(link for link in email.links if "view-in-browser" in link.url)
    assert not browser.invoice_likely and "negative" in browser.reasons


def test_forwarded_email_is_parsed_as_nested_message_with_thread_headers():
    email = parse_eml(fixture("forwarded_invoice.eml"))
    assert email.thread.in_reply_to == ("req-77@backoffice.example",)
    assert email.thread.references == ("chase-1@backoffice.example", "req-77@backoffice.example")
    assert email.thread.root == "chase-1@backoffice.example"
    (attached,) = email.attached_emails
    assert attached.parsed.subject == "Invoice 2026-0412"
    assert attached.parsed.sender_domain == "hazeltree.pt"
    assert [p.filename for p in attached.parsed.attachments] == ["FT_2026-0412.pdf"]


def test_malformed_message_never_crashes():
    email = parse_eml(fixture("malformed.eml"))
    assert email.date is None  # "not a real date"
    assert email.thread.message_id == "bare-id-without-brackets@agua.pt"
    assert email.sender_domain == "xn--gua-hla.pt"
    assert email.subject.startswith("Fatura de")
    assert "https://agua.pt/f/123" in {link.url for link in email.links}  # trailing ")." trimmed
    assert email.attachments[0].filename == "....fatura.pdf"  # quoted-string escapes, no path
    assert email.defects >= 1


def test_nesting_depth_is_bounded():
    inner = EmailMessage()
    inner["Subject"] = "level 3"
    inner.set_content("deep")
    for level in (2, 1):
        outer = EmailMessage()
        outer["Subject"] = f"level {level}"
        outer.set_content("wrapper")
        outer.add_attachment(inner)
        inner = outer
    email = parse_eml(inner.as_bytes(policy=policy.SMTP), EmailLimits(max_depth=2))
    level2 = email.attached_emails[0].parsed
    assert level2.subject == "level 2"
    assert level2.attached_emails[0].parsed is None  # kept as bytes, not parsed further


def test_part_limit_marks_message_truncated():
    msg = EmailMessage()
    msg.set_content("many parts")
    for i in range(5):
        msg.add_attachment(PDF + bytes([i]), maintype="application", subtype="pdf", filename=f"{i}.pdf")
    email = parse_eml(msg.as_bytes(policy=policy.SMTP), EmailLimits(max_parts=3))
    assert email.truncated and len(email.attachments) == 2


def hostile_nesting(depth: int) -> bytes:
    parts = [b"From: a@supplier.pt\r\nSubject: deep\r\nMIME-Version: 1.0\r\n"]
    for i in range(depth):
        parts.append(b'Content-Type: multipart/mixed; boundary="b%d"\r\n\r\n--b%d\r\n' % (i, i))
    parts.append(b"Content-Type: text/plain\r\n\r\nhello\r\n")
    parts += [b"--b%d--\r\n" % i for i in reversed(range(depth))]
    return b"".join(parts)


def test_hostile_nesting_degrades_to_headers_and_keeps_the_original(ingestor):
    raw = hostile_nesting(3000)
    email = parse_eml(raw)
    assert email.unreadable and email.truncated and email.subject == "deep"
    result = ingestor.ingest(raw, tenant_id="t1")
    assert result.message.evidence.metadata["unreadable"] is True
    assert ingestor.registry.open("t1", result.message.evidence.id) == raw  # never lose the original (§3, §55)
    assert [s.reason for s in result.skipped] == ["unreadable_structure"]


def test_moderate_nesting_is_capped_by_our_own_limit():
    email = parse_eml(hostile_nesting(80), EmailLimits(max_nesting=10))
    assert email.truncated and not email.unreadable and email.text_body == ""


@pytest.mark.parametrize("raw", [b"", b"   ", "not bytes"])
def test_empty_or_wrong_input_is_refused(raw):
    with pytest.raises(EmailParseError):
        parse_eml(raw)  # type: ignore[arg-type]


def test_oversized_message_is_refused():
    with pytest.raises(EmailParseError):
        parse_eml(fixture("vodafone_invoice.eml"), EmailLimits(max_bytes=100))


# --------------------------------------------------------------------------- Gmail raw


def test_gmail_raw_base64url_without_padding():
    raw = fixture("link_only_invoice.eml")
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert decode_gmail_raw(encoded) == raw
    assert parse_gmail_raw(encoded).subject == "Your invoice is ready"
    assert decode_gmail_raw(encoded.encode()) == raw


@pytest.mark.parametrize("bad", ["", "abc$%", "a+b/c=="])
def test_gmail_raw_rejects_non_base64url(bad):
    with pytest.raises(EmailParseError):
        decode_gmail_raw(bad)


# --------------------------------------------------------------------------- link scoring


def test_score_link_rules():
    pdf_score, reasons = score_link("https://x.pt/f/1.pdf", "Descarregar fatura", sender_domain="x.pt")
    assert {"text:invoice", "text:action", "url:pdf", "sender_domain"} <= set(reasons)
    unsub, _ = score_link("https://x.pt/u", "Unsubscribe")
    social, _ = score_link("https://www.linkedin.com/company/x", "View invoice")
    assert pdf_score > 40 > unsub and social < 0


def test_protected_pdf_link_mentioning_a_password_is_still_an_invoice_link():
    html = '<a href="https://banco.pt/docs/extrato-09.pdf">Descarregar extrato (password: o seu NIF)</a>'
    (link,) = extract_links(html, "")
    assert "negative" in link.reasons and link.invoice_likely


def test_extract_links_dedupes_and_prefers_best_text():
    html = ('<a href="https://s.pt/inv/7">https://s.pt/inv/7</a>'
            '<a href="https://s.pt/inv/7" class="btn">Download invoice</a>'
            '<a href="javascript:alert(1)">View invoice</a><a href="cid:img">x</a>')
    links = extract_links(html, "See https://s.pt/inv/7.", sender_domain="s.pt")
    assert [link.url for link in links] == ["https://s.pt/inv/7"]
    assert links[0].text == "Download invoice" and links[0].button_like
    assert links[0].order == 0  # first appearance kept for stable ordering


def test_pathological_markup_is_read_linearly():
    from backoffice.evidence.html_signals import analyze_html

    html = "<div>" * 50_000 + "</p>" * 50_000 + '<td bgcolor="#e60000"><a href="https://s.example/i.pdf">Ver fatura</a></td>'
    anchor = analyze_html(html).anchors[-1]
    assert anchor.button_like and anchor.text == "Ver fatura"


def test_portuguese_and_spanish_button_vocabulary():
    for text in ("Ver fatura", "Descarregar", "Ver factura", "Descargar factura", "Download", "View invoice"):
        html = f'<td bgcolor="#e60000"><a href="https://s.example/doc/1">{text}</a></td>'
        (link,) = extract_links(html, "")
        assert link.button_like, text


# --------------------------------------------------------------------------- ingestion


def test_ingest_creates_evidence_for_message_and_every_file(ingestor):
    result = ingestor.ingest(fixture("vodafone_invoice.eml"), tenant_id="t1", received_at=T0)
    msg = result.message.evidence
    assert msg.format is EvidenceFormat.EMAIL and msg.mime_type == "message/rfc822"
    assert msg.metadata["sender_domain"] == "vodafone.pt"
    assert msg.metadata["invoice_links"][0].endswith("FT2026-183.pdf")
    files = {f.filename: f for f in result.files}
    pdf = files["Fatura FT 2026-183.pdf"]
    assert pdf.evidence.format is EvidenceFormat.PDF and not pdf.type_mismatch
    assert pdf.registration.sighting.context["parent_evidence_id"] == msg.id
    assert files["logo.png"].inline and files["logo.png"].evidence.format is EvidenceFormat.IMAGE
    assert len(result.all_evidence()) == 3
    assert ingestor.registry.open("t1", pdf.evidence.id).startswith(b"%PDF")


def test_ingest_forwarded_email_creates_nested_evidence(ingestor):
    result = ingestor.ingest(fixture("forwarded_invoice.eml"), tenant_id="t1")
    (nested,) = result.attached_emails
    assert nested.message.evidence.format is EvidenceFormat.EML
    assert nested.message.sighting.context["parent_evidence_id"] == result.message.evidence.id
    assert [f.filename for f in nested.files] == ["FT_2026-0412.pdf"]
    assert len(result.all_evidence()) == 3


def test_ingest_zip_attachment_expands_safely(ingestor):
    result = ingestor.ingest(fixture("zip_attachment.eml"), tenant_id="t1")
    by_name = {f.filename: f for f in result.files}
    assert by_name["faturas-agosto.zip"].evidence.format is EvidenceFormat.ZIP
    assert {"FT-001.pdf", "FT-002.pdf"} <= set(by_name)
    member = by_name["FT-001.pdf"]
    assert member.path == "2/faturas/FT-001.pdf"
    assert member.registration.sighting.context["archive_evidence_id"] == by_name["faturas-agosto.zip"].evidence.id
    reasons = {s.reason for s in result.skipped}
    assert {"archive:path_traversal", "archive:system_file"} <= reasons


def test_ingest_flags_mismatch_parses_octet_stream_eml_and_skips_unknown(ingestor):
    result = ingestor.ingest(fixture("mixed_attachments.eml"), tenant_id="t1")
    fake_pdf = next(f for f in result.files if f.filename == "invoice.pdf")
    assert fake_pdf.type_mismatch and fake_pdf.evidence.format is EvidenceFormat.HTML
    assert fake_pdf.evidence.metadata["declared_mime_type"] == "application/pdf"
    (nested,) = result.attached_emails
    assert nested.parsed.sender_domain == "uber.com"
    invite = next(f for f in result.files if f.filename == "invite.ics")
    assert invite.evidence.format is EvidenceFormat.TEXT  # kept: invites can carry deadlines (§24)
    assert result.skipped == ()


def test_unreadable_binary_attachment_is_reported_not_dropped(ingestor):
    m = EmailMessage()
    m["From"] = "a@supplier.pt"
    m.set_content("x")
    m.add_attachment(bytes(range(256)) * 8, maintype="application", subtype="octet-stream", filename="blob.bin")
    result = ingestor.ingest(m.as_bytes(policy=policy.SMTP), tenant_id="t1")
    assert [(s.filename, s.reason) for s in result.skipped] == [("blob.bin", "unsupported")]
    assert result.files == ()  # still preserved inside the original message evidence


def test_same_pdf_in_two_emails_is_one_evidence_with_two_sightings(ingestor):
    def email_with(pdf: bytes, subject: str) -> bytes:
        m = EmailMessage()
        m["From"] = "a@supplier.pt"
        m["Subject"] = subject
        m.set_content("x")
        m.add_attachment(pdf, maintype="application", subtype="pdf", filename="f.pdf")
        return m.as_bytes(policy=policy.SMTP)

    a = ingestor.ingest(email_with(PDF, "first"), tenant_id="t1")
    b = ingestor.ingest(email_with(PDF, "resend"), tenant_id="t1")
    assert a.files[0].evidence.id == b.files[0].evidence.id
    assert a.files[0].registration.created and not b.files[0].registration.created
    sightings = ingestor.registry.sightings("t1", a.files[0].evidence.id)
    assert [s.context["parent_evidence_id"] for s in sightings] == [a.message.evidence.id, b.message.evidence.id]


def test_reingesting_the_same_message_is_idempotent(ingestor):
    raw = fixture("vodafone_invoice.eml")
    first = ingestor.ingest(raw, tenant_id="t1")
    again = ingestor.ingest(raw, tenant_id="t1", source_kind=SourceKind.MOBILE_SHARE,
                            message_format=EvidenceFormat.EML)
    assert again.message.evidence == first.message.evidence and not again.message.created
    assert [f.evidence.id for f in again.files] == [f.evidence.id for f in first.files]


def test_ingest_gmail_raw(ingestor):
    encoded = base64.urlsafe_b64encode(fixture("link_only_invoice.eml")).decode().rstrip("=")
    result = ingestor.ingest_gmail_raw(encoded, tenant_id="t1")
    assert result.message.evidence.sha256 == result.parsed.raw_sha256
    assert result.links[0].url == "https://billing.acme-cloud.com/i/5521"
