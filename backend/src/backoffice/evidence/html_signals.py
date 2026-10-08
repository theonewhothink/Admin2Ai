"""Deterministic HTML reading for links, buttons and login walls (§8, §9).

Uses the stdlib tokenizer only; no JavaScript is executed and nothing is
fetched. The same signals serve two callers: email bodies (find the "View
invoice" button) and pages behind links (is this a login or a code prompt?).
Outlook "bulletproof buttons" live in conditional comments as VML
``<v:roundrect href=...>``; those are read too.

Hostile markup must not cost more than linear time (§52 untrusted input):
comment scanning never backtracks across the whole comment, button labels
are read from a bounded window, and at most ``max_anchors`` links are kept.
"""

from __future__ import annotations

import re
from collections import deque
from itertools import islice
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser

from backoffice.countries import LazyPattern, pack_words

__all__ = ["MAX_ANCHORS", "Anchor", "HtmlSignals", "analyze_html", "html_to_text", "normalize_space"]

_WS = re.compile(r"\s+")
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
     "source", "track", "wbr"}
)  # fmt: skip
_SKIP_TEXT = frozenset({"script", "style", "template", "svg"})
_MAX_STACK = 256
_BUTTON_ATTR = re.compile(r"\b(btn|button|cta)\b|button", re.IGNORECASE)
_BUTTON_STYLE = re.compile(r"border-radius|display\s*:\s*(inline-)?block|padding\s*:", re.IGNORECASE)
# A VML/anchor start tag inside a comment. ``[^<>]*`` stops at the next tag
# boundary, so a comment full of unclosed tags is still scanned in linear time.
_VML_TAG = re.compile(r"<(?:v:[a-z]+|a)\b([^<>]*)>", re.IGNORECASE)
_HREF_ATTR = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>|<!\[endif\]")
_VML_END = re.compile(r"</v:|</a\s*>|<!\[endif", re.IGNORECASE)
_VML_LABEL_WINDOW = 2_000  # characters read after a VML button tag for its label
MAX_ANCHORS = 5_000
_PLAIN_BG = frozenset({"", "#fff", "#ffffff", "white", "transparent", "none", "inherit", "initial"})
_IMPORTANT = re.compile(r"!\s*important", re.IGNORECASE)
_BG_VALUE = re.compile(r"background(?:-color)?\s*:\s*([^;]+)", re.IGNORECASE)
_OTP_NAME = re.compile(
    r"otp|mfa|2fa|totp|one[-_ ]?time|verif\w*[-_ ]?code|security[-_ ]?code|auth\w*[-_ ]?code"
    r"|sms[-_ ]?code|c[oó]digo(?![-_ ]?postal)|passcode|token",
    re.IGNORECASE,
)
_LOGIN_NAME = LazyPattern(lambda: (r"e-?mail|user(name)?|login|usuario|\bnif\b"  # a pack's own: "html.login_name"
                                   + "".join(f"|{w}" for w in pack_words("html.login_name"))), re.IGNORECASE)
_META_URL = re.compile(r"url\s*=\s*['\"]?([^'\"]+)", re.IGNORECASE)
_META_DELAY = re.compile(r"\s*(\d{1,9})")


def _coloured(attrs: dict[str, str]) -> bool:
    """A non-white background: how email templates draw buttons."""
    values = [attrs.get("bgcolor", "")]
    values += [m.group(1) for m in _BG_VALUE.finditer(attrs.get("style", ""))]
    cleaned = (_IMPORTANT.sub("", v).strip().lower() for v in values)
    return any(v not in _PLAIN_BG for v in cleaned if v)


def normalize_space(text: str) -> str:
    return _WS.sub(" ", text).strip()


@dataclass(frozen=True)
class Anchor:
    href: str
    text: str
    button_like: bool
    order: int
    source: str = "a"  # "a", "area" or "vml"


@dataclass
class HtmlSignals:
    anchors: list[Anchor] = field(default_factory=list)
    text: str = ""
    title: str = ""
    base_href: str | None = None
    meta_refresh: str | None = None
    meta_refresh_delay: int = 0  # seconds before the refresh fires (0 = immediate)
    has_password_input: bool = False
    has_login_input: bool = False
    has_otp_input: bool = False
    form_actions: list[str] = field(default_factory=list)
    script_count: int = 0
    noscript_text: str = ""
    anchors_truncated: bool = False  # more than ``max_anchors`` links; the rest were ignored


class _Collector(HTMLParser):
    def __init__(self, max_text: int, max_anchors: int = MAX_ANCHORS) -> None:
        super().__init__(convert_charrefs=True)
        self.signals = HtmlSignals()
        self._max_text = max_text
        self._max_anchors = max_anchors
        self._text: list[str] = []
        self._text_len = 0
        self._stack: deque[tuple[str, bool]] = deque()  # (tag, has background), newest last
        self._open: dict[str, int] = {}  # open-tag counts for the stack
        self._skip = 0
        self._in_title = False
        self._in_noscript = False
        self._noscript: list[str] = []
        self._title: list[str] = []
        self._anchor: dict | None = None
        self._order = 0
        self._handlers = {
            "a": self._start_a, "area": self._start_area, "img": self._start_img, "base": self._start_base,
            "meta": self._start_meta, "form": self._start_form, "input": self._start_input,
        }

    # ----------------------------------------------------------------- tags

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {k.lower(): (v or "") for k, v in attrs_list}
        if tag in _SKIP_TEXT:
            self._skip += 1
            self.signals.script_count += tag == "script"
        elif tag == "title":
            self._in_title = True
        elif tag == "noscript":
            self._in_noscript = True
        handler = self._handlers.get(tag)
        if handler is not None:
            handler(attrs)
        if tag not in _VOID:
            self._push(tag, _coloured(attrs))

    def handle_startendtag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs_list)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TEXT and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "noscript":
            self._in_noscript = False
        elif tag in ("a", "area"):
            self._close_anchor()
        if self._open.get(tag):  # unmatched end tags cost nothing
            while self._stack:
                open_tag, _ = self._stack.pop()
                self._open[open_tag] -= 1
                if open_tag == tag:
                    break

    def _push(self, tag: str, coloured: bool) -> None:
        """Bounded ancestor stack: only near ancestors matter for button detection."""
        if len(self._stack) >= _MAX_STACK:
            evicted, _ = self._stack.popleft()
            self._open[evicted] -= 1
        self._stack.append((tag, coloured))
        self._open[tag] = self._open.get(tag, 0) + 1

    def _start_a(self, attrs: dict[str, str]) -> None:
        self._close_anchor()  # nested anchors are invalid: close the previous one
        href = attrs.get("href", "").strip()
        if not href or self._anchors_full():
            return
        self._anchor = {"href": href, "text": [], "button": self._button_like(attrs), "source": "a"}

    def _start_area(self, attrs: dict[str, str]) -> None:
        href = attrs.get("href", "").strip()
        if href and not self._anchors_full():
            self._add_anchor(href, attrs.get("alt", ""), False, "area")

    def _start_img(self, attrs: dict[str, str]) -> None:
        if self._anchor is not None and attrs.get("alt"):
            self._anchor["text"].append(attrs["alt"])

    def _start_base(self, attrs: dict[str, str]) -> None:
        if attrs.get("href") and self.signals.base_href is None:
            self.signals.base_href = attrs["href"].strip()

    def _start_meta(self, attrs: dict[str, str]) -> None:
        if attrs.get("http-equiv", "").lower() == "refresh":
            content = attrs.get("content", "")
            m = _META_URL.search(content)
            if m and self.signals.meta_refresh is None:
                self.signals.meta_refresh = m.group(1).strip()
                delay = _META_DELAY.match(content)
                self.signals.meta_refresh_delay = int(delay.group(1)) if delay else 0

    def _start_form(self, attrs: dict[str, str]) -> None:
        self.signals.form_actions.append(attrs.get("action", "").strip())

    def _start_input(self, attrs: dict[str, str]) -> None:
        kind = attrs.get("type", "text").lower()
        names = " ".join(attrs.get(k, "") for k in ("name", "id", "placeholder", "aria-label"))
        if kind == "password":
            self.signals.has_password_input = True
        if kind == "hidden":
            return
        if attrs.get("autocomplete", "").lower() == "one-time-code" or _OTP_NAME.search(names):
            self.signals.has_otp_input = True
        elif kind == "email" or _LOGIN_NAME.search(names):
            self.signals.has_login_input = True

    def _button_like(self, attrs: dict[str, str]) -> bool:
        marker = " ".join(attrs.get(k, "") for k in ("class", "id", "role"))
        if _BUTTON_ATTR.search(marker):
            return True
        if _coloured(attrs) and _BUTTON_STYLE.search(attrs.get("style", "")):
            return True
        # Table-cell buttons: a coloured cell right around the link.
        return any(has_bg for _, has_bg in islice(reversed(self._stack), 2))

    # ----------------------------------------------------------------- anchors

    def _anchors_full(self) -> bool:
        if len(self.signals.anchors) >= self._max_anchors:
            self.signals.anchors_truncated = True
            return True
        return False

    def _add_anchor(self, href: str, text: str, button: bool, source: str) -> None:
        self.signals.anchors.append(
            # Attribute values are already unescaped by the tokenizer; VML comes raw.
            Anchor(href=unescape(href) if source == "vml" else href,
                   text=normalize_space(unescape(text) if source == "vml" else text)[:200],
                   button_like=button,
                   order=self._order, source=source)
        )
        self._order += 1

    def _close_anchor(self) -> None:
        if self._anchor is not None:
            a = self._anchor
            self._anchor = None
            self._add_anchor(a["href"], " ".join(a["text"]), a["button"], a["source"])

    # ----------------------------------------------------------------- text

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._in_title:
            self._title.append(data)
        if self._in_noscript:
            self._noscript.append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)
        if self._text_len < self._max_text:
            self._text.append(data)
            self._text_len += len(data)

    def handle_comment(self, data: str) -> None:
        """Outlook conditional comments carry VML buttons: ``<v:roundrect href="...">``."""
        if "href" not in data.lower():
            return
        for m in _VML_TAG.finditer(data):
            attr = _HREF_ATTR.search(m.group(1))
            if attr is None:
                continue
            if self._anchors_full():
                return
            href = attr.group(1) if attr.group(1) is not None else attr.group(2)
            window_end = min(len(data), m.end() + _VML_LABEL_WINDOW)
            end = _VML_END.search(data, m.end(), window_end)
            label = _TAG.sub(" ", data[m.end() : end.start() if end else window_end])
            self._add_anchor(href.strip(), label, True, "vml")

    def finish(self) -> HtmlSignals:
        self._close_anchor()
        s = self.signals
        s.text = normalize_space(" ".join(self._text))[: self._max_text]
        s.title = normalize_space(" ".join(self._title))[:300]
        s.noscript_text = normalize_space(" ".join(self._noscript))[:1000]
        return s


def analyze_html(html: str, *, max_text: int = 200_000, max_anchors: int = MAX_ANCHORS) -> HtmlSignals:
    """Read anchors, visible text and form signals from ``html``. Never raises."""
    collector = _Collector(max_text, max_anchors)
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # noqa: BLE001 - malformed markup must not stop ingestion
        pass
    return collector.finish()


# --------------------------------------------------------------------------- text of an HTML body

# Elements that start a new line in what a reader sees ("Total: 12,30 €" stays on its own line).
_BLOCK = frozenset(
    {"address", "article", "aside", "blockquote", "br", "caption", "dd", "div", "dl", "dt", "fieldset",
     "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li",
     "main", "nav", "ol", "p", "pre", "section", "table", "tbody", "tfoot", "thead", "tr", "ul"}
)  # fmt: skip
_CELL = frozenset({"td", "th"})
_BLANK_LINES = re.compile(r"\n{3,}")
_INLINE_WS = re.compile(r"[^\S\n]+")


class _TextCollector(HTMLParser):
    """Visible text with line breaks where the page breaks lines; scripts, styles and comments dropped."""

    def __init__(self, max_chars: int) -> None:
        super().__init__(convert_charrefs=True)
        self._max = max_chars
        self._out: list[str] = []
        self._size = 0
        self._skip = 0

    def _emit(self, text: str) -> None:
        if self._size < self._max:
            self._out.append(text)
            self._size += len(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TEXT or tag in ("head", "title", "noscript"):
            self._skip += 1
        elif tag in _BLOCK:
            self._emit("\n")
        elif tag in _CELL:
            self._emit(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK:
            self._emit("\n")

    def handle_endtag(self, tag: str) -> None:
        if (tag in _SKIP_TEXT or tag in ("head", "title", "noscript")) and self._skip:
            self._skip -= 1
        elif tag in _BLOCK:
            self._emit("\n")
        elif tag in _CELL:
            self._emit(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._emit(data)

    def text(self) -> str:
        joined = "".join(self._out)[: self._max]
        lines = (_INLINE_WS.sub(" ", line).strip() for line in joined.split("\n"))
        return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def html_to_text(html: str, *, max_chars: int = 200_000) -> str:
    """The text a person reads in an HTML email or page, one line per visual line (§8).

    Stdlib tokenizer only: nothing is fetched or executed, scripts, styles,
    the head and comments are dropped, and the output is bounded. Never raises.
    """
    if not html:
        return ""
    collector = _TextCollector(max_chars)
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # noqa: BLE001 - malformed markup must not stop ingestion
        pass
    return collector.text()
