"""Safe XML parsing for untrusted structured evidence (§13 Stage 0, §52).

Invoices arrive from anyone who can send an email. A document carrying a
DOCTYPE, entity, notation or external-entity declaration is rejected outright
(no billion laughs, no external entity expansion), and size and nesting depth
are bounded. Built directly on expat; the result is an ordinary ElementTree
with ``{namespace}local`` tags.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from typing import NoReturn
from xml.parsers import expat

from .fields import StructuredDataError

__all__ = [
    "MAX_XML_BYTES",
    "MAX_XML_DEPTH",
    "UnsafeXMLError",
    "XMLSyntaxError",
    "parse_xml",
]

MAX_XML_BYTES = 20 * 1024 * 1024
MAX_XML_DEPTH = 200

_NS_SEPARATOR = " "  # never valid inside a namespace URI or an XML name


class UnsafeXMLError(StructuredDataError):
    """The XML uses a feature we refuse to process (DTD, entities) or is too big."""


class XMLSyntaxError(StructuredDataError):
    """The XML is not well-formed."""


def _clark(name: str) -> str:
    uri, sep, local = name.rpartition(_NS_SEPARATOR)
    return f"{{{uri}}}{local}" if sep else name


def _refuse(code: str) -> Callable[..., NoReturn]:
    def handler(*_: object) -> NoReturn:
        raise UnsafeXMLError(code)

    return handler


def parse_xml(
    data: bytes | str, *, max_bytes: int = MAX_XML_BYTES, max_depth: int = MAX_XML_DEPTH
) -> ET.Element:
    """Parse untrusted XML into an Element, refusing DTDs and entities."""
    size = len(data.encode("utf-8")) if isinstance(data, str) else len(data)
    if size > max_bytes:
        raise UnsafeXMLError("xml_too_large", f"{size} bytes")

    builder = ET.TreeBuilder()
    parser = expat.ParserCreate(namespace_separator=_NS_SEPARATOR)
    depth = 0

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth
        depth += 1
        if depth > max_depth:
            raise UnsafeXMLError("xml_too_deep")
        builder.start(_clark(name), {_clark(k): v for k, v in attrs.items()})

    def end(name: str) -> None:
        nonlocal depth
        depth -= 1
        builder.end(_clark(name))

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = builder.data
    parser.StartDoctypeDeclHandler = _refuse("xml_doctype")
    parser.EntityDeclHandler = _refuse("xml_entity")
    parser.UnparsedEntityDeclHandler = _refuse("xml_entity")
    parser.NotationDeclHandler = _refuse("xml_notation")
    parser.ExternalEntityRefHandler = _refuse("xml_external_entity")
    parser.SetParamEntityParsing(expat.XML_PARAM_ENTITY_PARSING_NEVER)
    parser.buffer_text = True
    try:
        parser.Parse(data, True)
    except expat.ExpatError as exc:
        raise XMLSyntaxError("xml_syntax", expat.ErrorString(exc.code)) from None
    return builder.close()
