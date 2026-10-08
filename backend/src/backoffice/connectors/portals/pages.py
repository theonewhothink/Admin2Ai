"""Reading a supplier website's pages: a small HTML tree and the CSS selectors adapters are configured with.

Standard library only (``html.parser``): no browser, no script runs, nothing is fetched. Supported selectors are
the ones an invoice list needs, nothing more:

* type ``td``, universal ``*``, ``#id``, ``.class`` (several), attributes ``[a]``, ``[a=v]``, ``[a="v"]``,
  ``[a~=v]``, ``[a^=v]``, ``[a$=v]``, ``[a*=v]``;
* the descendant (space) and child (``>``) combinators, and groups (``a, b``).

Anything else (pseudo-classes, sibling combinators) is refused with ``ValueError`` when the selector is compiled,
so a configuration mistake shows at once, never as an empty list.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from html.parser import HTMLParser

__all__ = ["Node", "parse_html", "select", "select_one"]

_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
                   "track", "wbr"})
# Elements whose end tag pages often leave out: a new one closes the open one (up to its container).
_IMPLIED: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "tr": (frozenset({"tr", "td", "th"}), frozenset({"table", "thead", "tbody", "tfoot"})),
    "td": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "th": (frozenset({"td", "th"}), frozenset({"tr", "table"})),
    "li": (frozenset({"li"}), frozenset({"ul", "ol"})),
    "option": (frozenset({"option"}), frozenset({"select", "datalist"})),
    "p": (frozenset({"p"}), frozenset({"div", "section", "article", "main", "body", "td", "li", "form"})),
    "tbody": (frozenset({"thead", "tbody", "tr", "td", "th"}), frozenset({"table"})),
}
_BLOCK_STOPS = frozenset({"div", "section", "article", "main", "body", "td", "th", "li", "form", "table"})
for _block in ("div", "table", "ul", "ol", "form", "section", "nav", "h1", "h2", "h3", "h4", "h5", "h6"):
    _IMPLIED.setdefault(_block, (frozenset({"p"}), _BLOCK_STOPS))  # a block closes an open paragraph
MAX_NODES = 50_000  # a page larger than this is cut (a list page is far smaller)


@dataclass(eq=False)
class Node:
    """One element. ``children`` holds elements and text in document order."""

    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list[Node | str] = field(default_factory=list)
    parent: Node | None = field(default=None, repr=False)

    def attr(self, name: str) -> str | None:
        return self.attrs.get(name.lower())

    @property
    def classes(self) -> tuple[str, ...]:
        return tuple((self.attrs.get("class") or "").split())

    def elements(self) -> Iterator[Node]:
        """Every element below this one, in document order."""
        stack = [c for c in reversed(self.children) if isinstance(c, Node)]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(c for c in reversed(node.children) if isinstance(c, Node))

    def text(self) -> str:
        """The element's words, spaces collapsed."""
        parts: list[str] = []

        def walk(node: Node) -> None:
            for c in node.children:
                if isinstance(c, str):
                    parts.append(c)
                elif c.tag not in ("script", "style", "template"):
                    walk(c)
                    if c.tag in ("br", "p", "div", "li", "tr", "td", "th"):
                        parts.append(" ")

        walk(self)
        return " ".join("".join(parts).split())

    def select(self, selector: str) -> list[Node]:
        return select(self, selector)

    def select_one(self, selector: str) -> Node | None:
        return select_one(self, selector)

    def form_fields(self) -> dict[str, str]:
        """A form's own fields as a browser would submit them untouched: hidden inputs (a CSRF token), text
        inputs with a value, checked boxes, selected options. Never a password or a button."""
        out: dict[str, str] = {}
        for el in self.elements():
            name = el.attr("name")
            if not name or el.attr("disabled") is not None:
                continue
            if el.tag == "input":
                kind = (el.attr("type") or "text").lower()
                if kind in ("password", "submit", "button", "image", "reset", "file"):
                    continue
                if kind in ("checkbox", "radio") and el.attr("checked") is None:
                    continue
                out[name] = el.attr("value") or ("on" if kind in ("checkbox", "radio") else "")
            elif el.tag == "select":
                chosen = next((o for o in el.elements() if o.tag == "option" and o.attr("selected") is not None),
                              None) or next((o for o in el.elements() if o.tag == "option"), None)
                if chosen is not None:
                    out[name] = chosen.attr("value") if chosen.attr("value") is not None else chosen.text()
            elif el.tag == "textarea":
                out[name] = el.text()
        return out


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]
        self.count = 0

    def _open(self, tag: str, attrs: list[tuple[str, str | None]], *, void: bool) -> None:
        if self.count >= MAX_NODES:
            return
        implied = _IMPLIED.get(tag)
        if implied is not None:  # close the open sibling (and what it holds), never past its container
            closes, stops = implied
            cut = None
            for i in range(len(self.stack) - 1, 0, -1):
                t = self.stack[i].tag
                if t in stops:
                    break
                if t in closes:
                    cut = i
            if cut is not None:
                del self.stack[cut:]
        parent = self.stack[-1]
        node = Node(tag, {k.lower(): (v if v is not None else "") for k, v in attrs}, [], parent)
        parent.children.append(node)
        self.count += 1
        if not void:
            self.stack.append(node)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, void=tag in _VOID)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, void=True)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def parse_html(text: str) -> Node:
    """The page as a tree (best effort, like a browser: unclosed rows and cells are closed for it)."""
    builder = _Builder()
    builder.feed(text)
    builder.close()
    return builder.root


# --------------------------------------------------------------------------- selectors


@dataclass(frozen=True)
class _Attr:
    name: str
    op: str  # "" (present), "=", "~=", "^=", "$=", "*="
    value: str

    def matches(self, node: Node) -> bool:
        have = node.attrs.get(self.name)
        if have is None:
            return False
        if self.op == "":
            return True
        if self.op == "=":
            return have == self.value
        if self.op == "~=":
            return self.value in have.split()
        if not self.value:
            return False
        if self.op == "^=":
            return have.startswith(self.value)
        if self.op == "$=":
            return have.endswith(self.value)
        return self.value in have  # "*="


@dataclass(frozen=True)
class _Compound:
    tag: str | None
    ids: tuple[str, ...]
    classes: tuple[str, ...]
    attrs: tuple[_Attr, ...]

    def matches(self, node: Node) -> bool:
        if self.tag is not None and node.tag != self.tag:
            return False
        if any(node.attrs.get("id") != i for i in self.ids):
            return False
        have = node.classes
        return all(c in have for c in self.classes) and all(a.matches(node) for a in self.attrs)


_NAME = r"-?[A-Za-z_][\w-]*"
_TOKEN = re.compile(
    rf"(?P<ws>\s+)|(?P<comb>>)|(?P<tag>\*|{_NAME})|#(?P<id>{_NAME})|\.(?P<cls>{_NAME})"
    rf"|\[\s*(?P<an>{_NAME})\s*(?:(?P<op>[~^$*]?=)\s*(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>[^\]\s]+))\s*)?\]"
)


def _group(part: str, selector: str) -> tuple[tuple[_Compound, ...], tuple[str, ...]]:
    """One selector of a group: its compounds and the combinators between them."""
    compounds: list[_Compound] = []
    combinators: list[str] = []
    tag: str | None = None
    ids: list[str] = []
    classes: list[str] = []
    attrs: list[_Attr] = []
    open_ = False  # a compound is being read
    joiner: str | None = None  # the combinator before the next compound

    def refuse() -> ValueError:
        return ValueError(f"unsupported selector {selector!r}")

    def close() -> None:
        nonlocal tag, open_
        compounds.append(_Compound(tag, tuple(ids), tuple(classes), tuple(attrs)))
        tag, open_ = None, False
        ids.clear(), classes.clear(), attrs.clear()

    pos = 0
    while pos < len(part):
        m = _TOKEN.match(part, pos)
        if m is None:
            raise refuse()
        pos = m.end()
        if m.group("ws") is not None:
            if open_:
                close()
                joiner = " "
            continue
        if m.group("comb") is not None:
            if open_:
                close()
            if not compounds:
                raise refuse()
            joiner = ">"
            continue
        if not open_:
            if compounds:
                combinators.append(joiner or " ")
            open_, joiner = True, None
        if m.group("tag") is not None:
            if tag is not None or ids or classes or attrs:
                raise refuse()  # a type comes first in a compound
            tag = None if m.group("tag") == "*" else m.group("tag").lower()  # "*": every element
        elif m.group("id") is not None:
            ids.append(m.group("id"))
        elif m.group("cls") is not None:
            classes.append(m.group("cls"))
        else:
            value = next((v for v in (m.group("dq"), m.group("sq"), m.group("bare")) if v is not None), "")
            attrs.append(_Attr(m.group("an").lower(), m.group("op") or "", value))
    if open_:
        close()
    if not compounds or joiner == ">":
        raise refuse()
    return tuple(compounds), tuple(combinators)


@lru_cache(maxsize=256)
def _compile(selector: str) -> tuple[tuple[tuple[_Compound, ...], tuple[str, ...]], ...]:
    """``(compounds, combinators)`` per group; combinators[i] joins compounds[i] and compounds[i + 1]."""
    groups = []
    for part in selector.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty selector in {selector!r}")
        groups.append(_group(part, selector))
    return tuple(groups)


def _matches(node: Node, compounds: tuple[_Compound, ...], combinators: tuple[str, ...], i: int) -> bool:
    if not compounds[i].matches(node):
        return False
    if i == 0:
        return True
    parent = node.parent
    if combinators[i - 1] == ">":
        return parent is not None and parent.tag != "#document" and _matches(parent, compounds, combinators, i - 1)
    while parent is not None and parent.tag != "#document":
        if _matches(parent, compounds, combinators, i - 1):
            return True
        parent = parent.parent
    return False


def select(root: Node, selector: str) -> list[Node]:
    """Every element below ``root`` matching ``selector``, in document order."""
    groups = _compile(selector)
    return [n for n in root.elements()
            if any(_matches(n, compounds, combinators, len(compounds) - 1) for compounds, combinators in groups)]


def select_one(root: Node, selector: str) -> Node | None:
    groups = _compile(selector)
    return next((n for n in root.elements()
                 if any(_matches(n, c, k, len(c) - 1) for c, k in groups)), None)
