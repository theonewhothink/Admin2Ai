"""Engine adapters against mocked servers (§14-17, §53)."""

import asyncio
import base64
import json
import os
import sys
import types
from decimal import Decimal

import httpx
import pytest

from backoffice.domain.models import ExtractionMethod
from backoffice.extraction import MissingDependencyError
from backoffice.ocr import (
    CommercialOCRConfig,
    CommercialOCRProvider,
    CommercialWireFormat,
    EndpointConfig,
    FakeOCRProvider,
    InProcessPaddleOCR,
    OCRHints,
    OCRInputError,
    OCRProviderInterface,
    OCRRejected,
    OCRResponseError,
    OCRUnavailable,
    PaddleOCRVLConfig,
    PaddleOCRVLProvider,
    PageImage,
    PPOCRConfig,
    PPOCRv6Provider,
    PPOCRVariant,
    RedactedInput,
    RedactionError,
    UnlimitedOCRConfig,
    UnlimitedOCRProvider,
    masked_pages_redactor,
    text_only_redactor,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x01" * 24
PNG2 = b"\x89PNG\r\n\x1a\n" + b"\x02" * 24
PNG3 = b"\x89PNG\r\n\x1a\n" + b"\x03" * 24
PDF = b"%PDF-1.7\n<< /Type /Page >>\n<< /Type /Page >>\n"


def run(coro):
    return asyncio.run(coro)


class Clock:
    """Advances 0.25 s per reading, so durations are deterministic."""

    def __init__(self):
        self.now = 100.0

    def __call__(self):
        self.now += 0.25
        return self.now


class Server:
    """Records requests and answers with a handler."""

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))

    def body(self, i=0):
        return json.loads(self.requests[i].content)


def json_reply(payload, status=200):
    return lambda request: httpx.Response(status, json=payload)


ENDPOINT = EndpointConfig("http://ocr.internal:8080/", timeout_seconds=5, api_key="s3cret")

# --------------------------------------------------------------------------- PP-OCRv6

PADDLEX_OCR = {
    "logId": "a1",
    "errorCode": 0,
    "errorMsg": "Success",
    "result": {
        "ocrResults": [
            {
                "prunedResult": {
                    "rec_texts": ["Invoice number: FT 2026/183", "Total: 483,60", "  "],
                    "rec_scores": [0.99, 0.93, 0.2],
                    "rec_polys": [
                        [[10, 10], [310, 10], [310, 30], [10, 30]],
                        [[10, 50], [210, 52], [210, 72], [10, 70]],
                        [[0, 0], [1, 0], [1, 1], [0, 1]],
                    ],
                    "rec_boxes": [[10, 10, 310, 30], [10, 50, 210, 72], [0, 0, 1, 1]],
                    "doc_preprocessor_res": {"angle": 180},
                },
                "ocrImage": None,
            }
        ],
        "dataInfo": {"type": "image", "width": 1240, "height": 1754},
    },
}


def test_ppocr_http_request_and_result():
    server = Server(json_reply(PADDLEX_OCR))
    provider = PPOCRv6Provider(
        PPOCRConfig(endpoint=ENDPOINT, cost_per_page=Decimal("0.0004")), client=server.client(), clock=Clock()
    )
    assert isinstance(provider, OCRProviderInterface)
    assert (provider.name, provider.version, provider.method) == (
        "pp-ocrv6-medium",
        "PP-OCRv6-medium",
        ExtractionMethod.OCR,
    )
    result = run(provider.recognize([PNG]))

    request = server.requests[0]
    assert str(request.url) == "http://ocr.internal:8080/ocr"
    assert request.headers["Authorization"] == "Bearer s3cret"
    assert server.body() == {"file": base64.b64encode(PNG).decode(), "fileType": 1, "visualize": False}

    [page] = result.pages
    assert (page.number, page.width, page.height) == (1, 1240, 1754)
    assert [line.text for line in page.lines] == ["Invoice number: FT 2026/183", "Total: 483,60"]
    first, second = page.lines
    assert (first.bbox.x0, first.bbox.y0, first.bbox.x1, first.bbox.y1, first.bbox.page) == (
        10,
        10,
        310,
        30,
        1,
    )
    assert first.confidence == 0.99 and first.angle == 0.0
    assert second.angle == pytest.approx(0.573, abs=0.01)
    assert page.layout.rotation == 180 and page.layout.inferred
    assert result.cost == Decimal("0.0004") and result.pages_billed == 1
    assert result.duration_ms == 250
    assert result.engine == "pp-ocrv6-medium"


def test_ppocr_pdf_pages_are_numbered_from_the_input():
    pdf_reply = {
        "errorCode": 0,
        "result": {
            "ocrResults": [
                {"prunedResult": {"rec_texts": ["page one"]}},
                {"prunedResult": {"rec_texts": ["page two"]}},
            ],
            "dataInfo": {
                "type": "pdf",
                "numPages": 2,
                "pages": [{"width": 600, "height": 800}, {"width": 610, "height": 810}],
            },
        },
    }
    server = Server(json_reply(pdf_reply))
    options = {"fileType": 1, "useDocUnwarping": True}  # options never override the input description
    config = PPOCRConfig(endpoint=ENDPOINT, cost_per_page=Decimal("0.001"), request_options=options)
    provider = PPOCRv6Provider(config, client=server.client())
    result = run(provider.recognize([PageImage(data=PDF, mime_type="application/pdf", number=3)]))
    assert server.body()["fileType"] == 0 and server.body()["useDocUnwarping"] is True
    assert [(p.number, p.width, p.text) for p in result.pages] == [(3, 600, "page one"), (4, 610, "page two")]
    assert result.cost == Decimal("0.002")


def test_ppocr_several_images_keep_order():
    replies = {PNG: "first", PNG2: "second", PNG3: "third"}

    def respond(request):
        data = base64.b64decode(json.loads(request.content)["file"])
        return httpx.Response(
            200,
            json={
                "errorCode": 0,
                "result": {"ocrResults": [{"prunedResult": {"rec_texts": [replies[data]]}}]},
            },
        )

    server = Server(respond)
    provider = PPOCRv6Provider(PPOCRConfig(endpoint=ENDPOINT, max_concurrency=2), client=server.client())
    result = run(provider.recognize([PNG, PNG2, PNG3]))
    assert [(p.number, p.text) for p in result.pages] == [(1, "first"), (2, "second"), (3, "third")]
    assert len(server.requests) == 3


def test_ppocr_accepts_legacy_hubserving_and_snake_case():
    hub = {
        "msg": "",
        "status": "000",
        "results": [
            [{"text": "Total 1,00", "confidence": 0.8, "text_region": [[0, 0], [90, 0], [90, 10], [0, 10]]}]
        ],
    }
    provider = PPOCRv6Provider(PPOCRConfig(endpoint=ENDPOINT), client=Server(json_reply(hub)).client())
    [page] = run(provider.recognize([PNG])).pages
    assert page.lines[0].text == "Total 1,00" and page.lines[0].confidence == 0.8
    snake = {"result": {"ocr_results": [{"pruned_result": {"rec_texts": ["x"], "rec_scores": [7.0]}}]}}
    provider = PPOCRv6Provider(PPOCRConfig(endpoint=ENDPOINT), client=Server(json_reply(snake)).client())
    [page] = run(provider.recognize([PNG])).pages
    assert page.lines[0].confidence is None  # out-of-range score is unknown, not clamped
    empty = {"errorCode": 0, "result": {"ocrResults": []}}
    provider = PPOCRv6Provider(PPOCRConfig(endpoint=ENDPOINT), client=Server(json_reply(empty)).client())
    result = run(provider.recognize([PNG]))
    assert result.pages[0].lines == () and result.pages_billed == 1


@pytest.mark.parametrize(
    ("respond", "error", "retryable"),
    [
        (json_reply({}, 503), OCRUnavailable, True),
        (json_reply({}, 429), OCRUnavailable, True),
        (json_reply({"errorMsg": "bad"}, 422), OCRRejected, False),
        (json_reply({"errorCode": 500, "errorMsg": "boom"}), OCRResponseError, False),
        (json_reply({"hubserving": True, "status": "101", "results": []}), OCRResponseError, False),
        (json_reply({"result": {"nothing": 1}}), OCRResponseError, False),
        (lambda r: httpx.Response(200, content=b"<html>proxy error</html>"), OCRResponseError, False),
    ],
)
def test_ppocr_failures_are_typed(respond, error, retryable):
    provider = PPOCRv6Provider(PPOCRConfig(endpoint=ENDPOINT), client=Server(respond).client())
    with pytest.raises(error) as info:
        run(provider.recognize([PNG]))
    assert info.value.retryable is retryable
    assert "proxy error" not in str(info.value)  # bodies never leak into errors


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
def test_ppocr_transport_errors_are_retryable(exc):
    def respond(request):
        raise exc

    provider = PPOCRv6Provider(PPOCRConfig(endpoint=ENDPOINT), client=Server(respond).client())
    with pytest.raises(OCRUnavailable):
        run(provider.recognize([PNG]))


def test_ppocr_input_checks_happen_before_any_request():
    server = Server(json_reply(PADDLEX_OCR))
    provider = PPOCRv6Provider(PPOCRConfig(endpoint=ENDPOINT, max_pages=1), client=server.client())
    with pytest.raises(OCRInputError):
        run(provider.recognize([PageImage(data=b"text", mime_type="text/plain")]))
    with pytest.raises(OCRInputError):
        run(provider.recognize([PNG, PNG2]))
    assert server.requests == []


def test_ppocr_needs_exactly_one_backend_and_names_variants():
    with pytest.raises(ValueError):
        PPOCRv6Provider(PPOCRConfig())
    with pytest.raises(ValueError):
        PPOCRv6Provider(PPOCRConfig(endpoint=ENDPOINT), local=InProcessPaddleOCR())
    tiny = PPOCRv6Provider(PPOCRConfig(variant=PPOCRVariant.TINY, endpoint=ENDPOINT))
    assert tiny.name == "pp-ocrv6-tiny"
    assert tiny.capabilities.accepts_pdf and not tiny.capabilities.external
    with pytest.raises(ValueError):
        EndpointConfig("ftp://x")
    with pytest.raises(ValueError):
        PPOCRConfig(cost_per_page=Decimal("-1"))


def test_in_process_paddleocr_is_optional_and_lazy(monkeypatch):
    monkeypatch.setitem(sys.modules, "paddleocr", None)
    provider = PPOCRv6Provider(local=InProcessPaddleOCR())
    with pytest.raises(MissingDependencyError):
        run(provider.recognize([PNG]))

    seen = {}

    class Result:
        def __init__(self, text):
            self.json = {"res": {"rec_texts": [text], "rec_scores": [0.9], "rec_boxes": [[0, 0, 50, 10]]}}

    class PaddleOCR:
        def __init__(self, **kwargs):
            seen["init"] = kwargs

        def predict(self, path, **kwargs):
            seen["path"], seen["predict"] = path, kwargs
            with open(path, "rb") as fh:
                assert fh.read() == PNG
            return [Result("Total: 1,00")]

    module = types.ModuleType("paddleocr")
    module.PaddleOCR = PaddleOCR
    monkeypatch.setitem(sys.modules, "paddleocr", module)
    engine = InProcessPaddleOCR(
        init_kwargs={"text_recognition_model_name": "custom"}, predict_kwargs={"x": 1}
    )
    result = run(PPOCRv6Provider(local=engine).recognize([PNG]))
    assert result.pages[0].lines[0].text == "Total: 1,00"
    assert seen["init"] == {"text_recognition_model_name": "custom"} and seen["predict"] == {"x": 1}
    assert seen["path"].endswith(".png") and not os.path.exists(seen["path"])  # temp file removed


# --------------------------------------------------------------------------- PaddleOCR-VL

LAYOUT_REPLY = {
    "errorCode": 0,
    "result": {
        "layoutParsingResults": [
            {
                "prunedResult": {
                    "parsing_res_list": [
                        {
                            "block_label": "doc_title",
                            "block_content": "Invoice FT 2026/183",
                            "block_bbox": [50, 40, 500, 70],
                            "block_order": 1,
                        },
                        {
                            "block_label": "table",
                            "block_content": (
                                "<table><tr><th>Item</th><th>Total</th></tr>"
                                "<tr><td>Total</td><td>483,60</td></tr></table>"
                            ),
                            "block_bbox": [50, 300, 560, 400],
                            "block_order": 3,
                        },
                        {
                            "block_label": "text",
                            "block_content": "Supplier VAT: PT509123457\nDate: 2026-09-18",
                            "block_bbox": [50, 100, 300, 140],
                            "block_order": 2,
                        },
                        {
                            "block_label": "image",
                            "block_content": "",
                            "block_bbox": [400, 40, 560, 90],
                            "block_order": 4,
                        },
                    ],
                    "width": 1240,
                    "height": 1754,
                },
                "markdown": {"text": "# Invoice FT 2026/183"},
            }
        ]
    },
}


def test_paddle_vl_blocks_become_lines_in_reading_order():
    server = Server(json_reply(LAYOUT_REPLY))
    provider = PaddleOCRVLProvider(
        PaddleOCRVLConfig(endpoint=ENDPOINT), client=server.client(), clock=Clock()
    )
    result = run(provider.recognize([PNG]))
    assert str(server.requests[0].url) == "http://ocr.internal:8080/layout-parsing"
    assert server.body()["fileType"] == 1 and server.body()["visualize"] is False
    [page] = result.pages
    assert [line.text for line in page.lines] == [
        "Invoice FT 2026/183",
        "Supplier VAT: PT509123457",
        "Date: 2026-09-18",
        "Item | Total",
        "Total | 483,60",
    ]
    assert page.lines[-1].bbox.y0 == 300 and page.lines[-1].confidence is None
    assert page.layout.tables == 1 and not page.layout.inferred
    assert page.markdown == "# Invoice FT 2026/183"
    assert (page.width, page.height) == (1240, 1754)
    assert result.method is ExtractionMethod.VLM and result.model_version == "PaddleOCR-VL-1.6"
    assert provider.capabilities.handles_tables


def test_paddle_vl_rejects_unexpected_shapes():
    for reply in (
        {"errorCode": 0, "result": {}},
        {"errorCode": 0, "result": {"layoutParsingResults": ["x"]}},
        [],
    ):
        provider = PaddleOCRVLProvider(
            PaddleOCRVLConfig(endpoint=ENDPOINT), client=Server(json_reply(reply)).client()
        )
        with pytest.raises(OCRResponseError):
            run(provider.recognize([PNG]))


# --------------------------------------------------------------------------- Unlimited-OCR


def chat_reply(content, finish="stop"):
    return {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}
        ]
    }


def test_unlimited_openai_request_and_page_split():
    server = Server(
        json_reply(chat_reply("=== PAGE 1 ===\nInvoice number: X-1\n=== PAGE 2 ===\nTotal: 10,00"))
    )
    provider = UnlimitedOCRProvider(
        UnlimitedOCRConfig(
            endpoint=EndpointConfig("http://vllm:8000"),
            model="unlimited-ocr-1",
            cost_per_page=Decimal("0.002"),
        ),
        client=server.client(),
    )
    result = run(provider.recognize([PNG, PNG2]))
    body = server.body()
    assert str(server.requests[0].url) == "http://vllm:8000/v1/chat/completions"
    assert body["model"] == "unlimited-ocr-1" and body["temperature"] == 0
    parts = body["messages"][0]["content"]
    assert [p["type"] for p in parts] == ["image_url", "image_url", "text"]
    assert parts[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[-1]["text"].endswith("Page numbers: 1, 2.")
    assert [(p.number, p.text) for p in result.pages] == [(1, "Invoice number: X-1"), (2, "Total: 10,00")]
    assert result.cost == Decimal("0.004") and result.warnings == ()
    assert provider.capabilities.handles_long_docs and result.method is ExtractionMethod.VLM


def test_unlimited_chunks_long_documents():
    def respond(request):
        text = json.loads(request.content)["messages"][0]["content"][-1]["text"]
        numbers = text.rsplit("Page numbers: ", 1)[1].rstrip(".").split(", ")
        return httpx.Response(200, json=chat_reply("\n".join(f"=== PAGE {n} ===\npage {n}" for n in numbers)))

    server = Server(respond)
    provider = UnlimitedOCRProvider(
        UnlimitedOCRConfig(endpoint=EndpointConfig("http://vllm:8000"), model="m", pages_per_request=2),
        client=server.client(),
    )
    result = run(provider.recognize([PNG, PNG2, PNG3]))
    assert len(server.requests) == 2
    assert [p.text for p in result.pages] == ["page 1", "page 2", "page 3"]


def test_unlimited_tolerates_missing_markers_parts_fences_and_truncation():
    content = [{"type": "text", "text": "```markdown\nall the text\n```"}]
    server = Server(json_reply(chat_reply(content, finish="length")))
    provider = UnlimitedOCRProvider(
        UnlimitedOCRConfig(endpoint=EndpointConfig("http://vllm:8000"), model="m"), client=server.client()
    )
    result = run(provider.recognize([PNG, PNG2]))
    assert [(p.number, p.text) for p in result.pages] == [(1, "all the text"), (2, "")]
    assert result.warnings == ("truncated", "page_boundaries_unknown")


def test_unlimited_pdf_as_file_part_or_rasterized():
    server = Server(json_reply(chat_reply("=== PAGE 1 ===\na\n=== PAGE 2 ===\nb")))
    provider = UnlimitedOCRProvider(
        UnlimitedOCRConfig(endpoint=EndpointConfig("http://vllm:8000"), model="m"), client=server.client()
    )
    result = run(provider.recognize([PDF]))
    part = server.body()["messages"][0]["content"][0]
    assert part["type"] == "file" and part["file"]["file_data"].startswith("data:application/pdf;base64,")
    assert [p.text for p in result.pages] == ["a", "b"]

    rasterized = []

    def rasterizer(pdf_page):
        rasterized.append(pdf_page.number)
        return [
            PageImage(data=PNG, mime_type="image/png", number=1),
            PageImage(data=PNG2, mime_type="image/png", number=2),
        ]

    server = Server(json_reply(chat_reply("=== PAGE 1 ===\na\n=== PAGE 2 ===\nb")))
    provider = UnlimitedOCRProvider(
        UnlimitedOCRConfig(endpoint=EndpointConfig("http://vllm:8000"), model="m"),
        client=server.client(),
        rasterizer=rasterizer,
    )
    run(provider.recognize([PDF]))
    assert rasterized == [1]
    assert [p["type"] for p in server.body()["messages"][0]["content"]] == ["image_url", "image_url", "text"]


def test_unlimited_bad_responses_and_config():
    provider = UnlimitedOCRProvider(
        UnlimitedOCRConfig(endpoint=EndpointConfig("http://vllm:8000"), model="m"),
        client=Server(json_reply({"error": "x"})).client(),
    )
    with pytest.raises(OCRResponseError):
        run(provider.recognize([PNG]))
    with pytest.raises(ValueError):
        UnlimitedOCRConfig(endpoint=EndpointConfig("http://vllm:8000"), model=" ")
    with pytest.raises(ValueError):
        UnlimitedOCRConfig(endpoint=EndpointConfig("http://vllm:8000"), model="m", pages_per_request=0)


# --------------------------------------------------------------------------- commercial

SECRET_IBAN = "PT50 0002 0123 1234 5678 9015 4"


class FakeRedaction:
    """Stands in for backoffice.policy.privacy.Redaction (text + restore)."""

    def __init__(self, text):
        self.text = text.replace(SECRET_IBAN, "[IBAN_1]")

    def restore(self, external_text):
        return external_text.replace("[IBAN_1]", SECRET_IBAN)


def commercial(server, redactor, **config):
    cfg = CommercialOCRConfig(
        endpoint=EndpointConfig("https://api.vendor.example", api_key="k"),
        model="vendor-ocr-2",
        cost_per_page=Decimal("0.015"),
        **config,
    )
    return CommercialOCRProvider(cfg, redactor=redactor, client=server.client(), clock=Clock())


def test_commercial_refuses_unredacted_pages_before_sending():
    server = Server(json_reply(chat_reply("x")))
    provider = commercial(server, lambda pages, hints: RedactedInput(pages=pages))
    with pytest.raises(RedactionError):
        run(provider.recognize([PNG]))
    assert server.requests == []
    assert provider.capabilities.external
    with pytest.raises(TypeError):
        CommercialOCRProvider(
            CommercialOCRConfig(
                endpoint=EndpointConfig("https://api.vendor.example"), model="m", cost_per_page=Decimal("0.01")
            ),
            redactor=None,  # type: ignore[arg-type]
        )


def test_commercial_text_only_redaction_and_local_restore():
    server = Server(json_reply(chat_reply("Total: 483,60\nIBAN: [IBAN_1]")))
    provider = commercial(server, text_only_redactor(FakeRedaction))
    hints = OCRHints(prior_text=f"Total 483,60\nIBAN {SECRET_IBAN}")
    result = run(provider.recognize([PNG], hints))
    sent = server.requests[0].content.decode()
    assert SECRET_IBAN not in sent and "[IBAN_1]" in sent
    assert base64.b64encode(PNG).decode() not in sent  # no image leaves
    assert [p["type"] for p in server.body()["messages"][0]["content"]] == ["text"]
    assert server.requests[0].headers["Authorization"] == "Bearer k"
    assert result.full_text == f"Total: 483,60\nIBAN: {SECRET_IBAN}"  # restored on our side
    assert result.cost == Decimal("0.015") and result.method is ExtractionMethod.VLM


def test_commercial_text_guard_and_missing_text():
    server = Server(json_reply(chat_reply("x")))
    leaky = commercial(
        server,
        text_only_redactor(lambda text: types.SimpleNamespace(text=text, restore=lambda t: t)),
        is_clean=lambda t: "PT50" not in t,
    )
    with pytest.raises(RedactionError):
        run(leaky.recognize([PNG], OCRHints(prior_text=SECRET_IBAN)))
    with pytest.raises(RedactionError):
        run(commercial(server, text_only_redactor(FakeRedaction)).recognize([PNG], OCRHints()))
    assert server.requests == []


def test_commercial_masked_pages_generic_json():
    reply = {
        "result": {
            "pages": [
                {
                    "number": 1,
                    "text": "Total 483,60",
                    "lines": [
                        {
                            "text": "Total 483,60",
                            "confidence": 97,
                            "bbox": {"x0": 1, "y0": 2, "x1": 30, "y1": 12},
                        },
                        {"text": "IBAN [IBAN_1]", "confidence": 88, "bbox": [1, 20, 30, 30]},
                        {"text": "  "},
                    ],
                }
            ]
        }
    }
    server = Server(json_reply(reply))
    masked = masked_pages_redactor(
        lambda page: PageImage(data=page.data + b"masked", mime_type=page.mime_type, number=page.number),
        FakeRedaction,
    )
    provider = commercial(server, masked, wire_format=CommercialWireFormat.GENERIC_JSON, confidence_scale=100)
    result = run(provider.recognize([PNG], OCRHints(prior_text=f"IBAN {SECRET_IBAN}", languages=("pt",))))
    body = server.body()
    assert str(server.requests[0].url) == "https://api.vendor.example/ocr"
    assert body["pages"][0]["data"] == base64.b64encode(PNG + b"masked").decode()
    assert body["text"] == "IBAN [IBAN_1]" and body["languages"] == ["pt"]
    lines = result.pages[0].lines
    assert [line.text for line in lines] == ["Total 483,60", f"IBAN {SECRET_IBAN}"]
    assert lines[0].confidence == 0.97 and lines[0].bbox.x1 == 30
    assert provider.capabilities.returns_boxes


def test_commercial_generic_json_plain_text_and_errors():
    provider = commercial(
        Server(json_reply({"text": "Total 1,00"})),
        masked_pages_redactor(lambda p: PageImage(data=p.data + b"m", mime_type=p.mime_type)),
        wire_format=CommercialWireFormat.GENERIC_JSON,
    )
    assert run(provider.recognize([PNG])).full_text == "Total 1,00"
    provider = commercial(
        Server(json_reply({"nothing": True})),
        masked_pages_redactor(lambda p: PageImage(data=p.data + b"m", mime_type=p.mime_type)),
        wire_format=CommercialWireFormat.GENERIC_JSON,
    )
    with pytest.raises(OCRResponseError):
        run(provider.recognize([PNG]))
    with pytest.raises(ValueError):
        CommercialOCRConfig(
            endpoint=EndpointConfig("https://api.vendor.example"), model="m", cost_per_page=Decimal("-0.01")
        )


def test_custom_api_key_header():
    config = EndpointConfig(
        "https://x.example", api_key="abc", api_key_header="x-api-key", headers={"X-Tenant": "t1"}
    )
    assert config.request_headers() == {"Accept": "application/json", "X-Tenant": "t1", "x-api-key": "abc"}
    assert "abc" not in repr(config)


# --------------------------------------------------------------------------- fake


def test_fake_provider_reads_text_pages_and_simulates_noise():
    fake = FakeOCRProvider("noisy", substitutions={"3": "8"}, cost_per_page=Decimal("0.01"), confidence=0.7)
    page = PageImage(data=b"Total: 483,60\n\nVAT: 90,43", mime_type="text/plain")
    result = run(fake.recognize([page], OCRHints(languages=("pt",))))
    assert result.full_text == "Total: 488,60\nVAT: 90,48"
    assert result.pages[0].lines[1].bbox.y0 == 20.0
    assert result.mean_confidence == pytest.approx(0.7)
    assert result.cost == Decimal("0.01")
    assert fake.calls[0][1].languages == ("pt",)
    fixed = FakeOCRProvider(texts=["only page"])
    assert run(fixed.recognize([PNG, PNG2])).full_text == "only page"
    failing = FakeOCRProvider(error=OCRUnavailable("fake"))
    with pytest.raises(OCRUnavailable):
        run(failing.recognize([PNG]))
    assert len(failing.calls) == 1
