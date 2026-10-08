"""OCR engine adapters, all behind ``OCRProviderInterface`` (§14-17).

* :class:`PPOCRv6Provider`: ``pp-ocrv6-tiny`` / ``pp-ocrv6-medium``, PaddleX
  ``/ocr`` serving or in-process PaddleOCR, method OCR;
* :class:`PaddleOCRVLProvider`: ``paddleocr-vl``, PaddleX ``/layout-parsing``,
  method VLM;
* :class:`UnlimitedOCRProvider`: ``unlimited-ocr``, vLLM OpenAI-compatible
  chat completions, method VLM;
* :class:`CommercialOCRProvider`: ``commercial``, OpenAI-compatible chat or a
  generic JSON API, method VLM by default;
* :class:`ClaudeVisionProvider`: ``claude-vision``, Anthropic Messages API with
  a forced tool call returning the critical fields as JSON, method VLM;
* :class:`LocalOCRProvider`: ``rapidocr``, PP-OCRv6 small (ONNX) in this process
  through RapidOCR, no sidecar and no network, method OCR;
* :class:`FakeOCRProvider`: in memory, for tests and the golden dataset.

Only the commercial and Claude providers are ``external``; both redact first (§53).
"""

from ._http import EndpointConfig
from .claude import CLAUDE_VISION, DEFAULT_VISION_MODEL, ClaudeVisionConfig, ClaudeVisionProvider
from .commercial import (
    CommercialOCRConfig,
    CommercialOCRProvider,
    CommercialWireFormat,
    RedactedInput,
    Redactor,
    TextRedaction,
    masked_pages_redactor,
    text_only_redactor,
)
from .fake import FakeOCRProvider
from .paddle_vl import PaddleOCRVLConfig, PaddleOCRVLProvider
from .ppocr import InProcessPaddleOCR, PPOCRConfig, PPOCRv6Provider, PPOCRVariant
from .rapid import LocalOCRConfig, LocalOCRProvider, local_ocr_available
from .unlimited import (
    DEFAULT_TRANSCRIBE_PROMPT,
    PdfPageRasterizer,
    Rasterizer,
    UnlimitedOCRConfig,
    UnlimitedOCRProvider,
)

__all__ = [
    "CLAUDE_VISION",
    "DEFAULT_VISION_MODEL",
    "ClaudeVisionConfig",
    "ClaudeVisionProvider",
    "DEFAULT_TRANSCRIBE_PROMPT",
    "CommercialOCRConfig",
    "CommercialOCRProvider",
    "CommercialWireFormat",
    "EndpointConfig",
    "FakeOCRProvider",
    "InProcessPaddleOCR",
    "LocalOCRConfig",
    "LocalOCRProvider",
    "PPOCRConfig",
    "PPOCRVariant",
    "PPOCRv6Provider",
    "PaddleOCRVLConfig",
    "PaddleOCRVLProvider",
    "PdfPageRasterizer",
    "Rasterizer",
    "RedactedInput",
    "Redactor",
    "TextRedaction",
    "UnlimitedOCRConfig",
    "UnlimitedOCRProvider",
    "local_ocr_available",
    "masked_pages_redactor",
    "text_only_redactor",
]
