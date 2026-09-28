"""OCR engine adapters, all behind ``OCRProviderInterface`` (§14-17).

* :class:`PPOCRv6Provider`: ``pp-ocrv6-tiny`` / ``pp-ocrv6-medium``, PaddleX
  ``/ocr`` serving or in-process PaddleOCR, method OCR;
* :class:`PaddleOCRVLProvider`: ``paddleocr-vl``, PaddleX ``/layout-parsing``,
  method VLM;
* :class:`UnlimitedOCRProvider`: ``unlimited-ocr``, vLLM OpenAI-compatible
  chat completions, method VLM;
* :class:`CommercialOCRProvider`: ``commercial``, OpenAI-compatible chat or a
  generic JSON API, method VLM by default;
* :class:`FakeOCRProvider`: in memory, for tests and the golden dataset.

Only the commercial provider is ``external``; it requires a redactor (§53).
"""

from ._http import EndpointConfig
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
from .unlimited import DEFAULT_TRANSCRIBE_PROMPT, Rasterizer, UnlimitedOCRConfig, UnlimitedOCRProvider

__all__ = [
    "DEFAULT_TRANSCRIBE_PROMPT",
    "CommercialOCRConfig",
    "CommercialOCRProvider",
    "CommercialWireFormat",
    "EndpointConfig",
    "FakeOCRProvider",
    "InProcessPaddleOCR",
    "PPOCRConfig",
    "PPOCRVariant",
    "PPOCRv6Provider",
    "PaddleOCRVLConfig",
    "PaddleOCRVLProvider",
    "Rasterizer",
    "RedactedInput",
    "Redactor",
    "TextRedaction",
    "UnlimitedOCRConfig",
    "UnlimitedOCRProvider",
    "masked_pages_redactor",
    "text_only_redactor",
]
