"""HTML fragments from document VLMs, turned into plain text lines.

Tables come back as ``<table>`` HTML. Each row becomes one line with cells
joined by " | ", so a label and its value stay on the same line, which is
what text field extractors rely on ("Total | 483,60").
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

__all__ = ["html_table_rows", "looks_like_html", "strip_tags"]

_TAG = re.compile(r"<\s*/?\s*[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?/?\s*>")


def looks_like_html(text: str) -> bool:
    return bool(_TAG.search(text))


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._close_cell()
            self.rows.append([])
        elif tag in ("td", "th"):
            self._close_cell()
            if not self.rows:
                self.rows.append([])
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th", "tr", "table"):
            self._close_cell()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def _close_cell(self) -> None:
        if self._cell is not None and self.rows:
            self.rows[-1].append(" ".join("".join(self._cell).split()))
        self._cell = None


def html_table_rows(html: str) -> list[list[str]]:
    """Non-empty rows of cell texts, in document order."""
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    parser._close_cell()
    return [row for row in parser.rows if any(cell for cell in row)]


class _TextParser(HTMLParser):
    _BLOCK = frozenset({"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_tags(text: str) -> str:
    """Plain text of an HTML fragment; text without tags is returned unchanged."""
    if not looks_like_html(text):
        return text
    parser = _TextParser()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)
