"""A fake EDP customer area (https://www.edp.pt/area-cliente) serving the recorded pages of
tests/fixtures/portals/edp through ``httpx.MockTransport``: sign-in with an anti-forgery field, an optional SMS code,
the invoice list per year (paginated), the PDFs, and the website signing a session out. No network.

It also gives the EDP invoices it lists (their text, read by :class:`SiteReader`, which stands in for the PDF text
layer) so the engine reads, verifies and matches what the website handed over.
"""

from __future__ import annotations

import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx

from backoffice.demo import evidence as E
from backoffice.domain.models import ExtractionMethod
from backoffice.reading import ReadOutcome
from backoffice.reading.stage0 import ReadStep, StepState

PAGES = Path(__file__).resolve().parent / "fixtures" / "portals" / "edp"
HOST = "www.edp.pt"
EMAIL = "ana@padaria.pt"
PASSWORD = "edp-password-1"
COOKIE = "edp_sessao"
NIF = "516123459"  # Padaria Lda in the server tests (the demo's Hazel Tree number)


def page(name: str, **values: Any) -> str:
    return string.Template((PAGES / name).read_text(encoding="utf-8")).safe_substitute(
        {k: str(v) for k, v in values.items()})


def edp_text(seq: int, day: date, net: str, vat: str, total: str) -> str:
    """An EDP invoice made out to Padaria Lda (its fiscal QR code agrees with the printed totals)."""
    def dot(v: str) -> str:
        return v.replace(",", ".")

    return (f"EDP Comercial - Comercialização de Energia, S.A.\nNIF: 501000100\nFatura n.º FT EDP2026/{seq}\n"
            f"ATCUD: EDPQ7K2M-{seq}\nData de emissão: {day:%d/%m/%Y}\n"
            f"Data de vencimento: {day + timedelta(days=1):%d/%m/%Y}\nCliente: Padaria Lda\nNIF: {NIF}\n"
            f"Eletricidade - loja\nBase tributável (23%): {net}\nIVA 23%: {vat}\nTotal: {total} €\n"
            f"Código QR: A:501000100*B:{NIF}*C:PT*D:FT*E:N*F:{day:%Y%m%d}*G:FT EDP2026/{seq}*H:EDPQ7K2M-{seq}*"
            f"I1:PT*I7:{dot(net)}*I8:{dot(vat)}*N:{dot(vat)}*O:{dot(total)}*Q:e1Dk*R:1422\n")


@dataclass
class Invoice:
    id: str
    number: str
    issued: date
    total: str  # as printed: "64,10"
    text: str  # what its PDF says
    period: str = ""

    @property
    def pdf(self) -> bytes:
        return b"%PDF-1.7\n% EDP " + self.number.encode() + b" (stand-in: the reader gives its text)\n%%EOF\n"


# The September electricity invoice (the demo's: FT EDP2026/558120, 64,10 EUR, paid on 19 September).
SEPTEMBER = Invoice("9001558120", "FT EDP2026/558120", date(2026, 9, 18), "64,10", E.EDP_INVOICE.decode(),
                    "18/08/2026 a 17/09/2026")
AUGUST = Invoice("9001544002", "FT EDP2026/544002", date(2026, 8, 18), "50,00",
                 edp_text(544002, date(2026, 8, 18), "40,65", "9,35", "50,00"), "18/07/2026 a 17/08/2026")
GAS = Invoice("9001560077", "FT EDP2026/560077", date(2026, 9, 25), "31,98",
              edp_text(560077, date(2026, 9, 25), "26,00", "5,98", "31,98"), "25/08/2026 a 24/09/2026")


class SiteReader:
    """Stands in for the PDF text layer of the invoices the website gives (counts every call; a replay must not)."""

    external_ai = False

    def __init__(self, *invoices: Invoice, fail: bool = False) -> None:
        self.texts = {i.pdf: i.text for i in invoices}
        self.fail = fail
        self.calls: list[Any] = []

    def engines(self) -> tuple[str, ...]:
        return ()

    def read(self, request: Any) -> ReadOutcome:
        self.calls.append(request)
        if self.fail:
            raise RuntimeError("a replay must never read")
        text = self.texts.get(request.data, "")
        return ReadOutcome(text=text, text_method=ExtractionMethod.EMBEDDED_TEXT, page_count=1,
                           steps=(ReadStep("pdf_text", StepState.DONE if text else StepState.NOTHING, "1 page"),))


class FakeEdpSite:
    """EDP's customer area (module docstring). ``invoices`` is what the list shows now."""

    def __init__(self, *, code: bool = False, page_size: int = 10,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.accounts = {EMAIL: PASSWORD}
        self.code = code
        self.page_size = page_size
        self.clock = clock or (lambda: datetime(2026, 10, 2, 9, 30, tzinfo=timezone.utc))
        self.invoices: list[Invoice] = []
        self.sessions: dict[str, dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []
        self.codes: list[str] = []
        self.down = False
        self.layout = "table"  # "cards": the area was redesigned (the adapter must say the website changed)
        self.download_redirect: str | None = None  # a PDF link that sends elsewhere
        self.passwords_seen: list[tuple[str, str]] = []  # (host, password) of every sign-in POST

    def client(self) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))

    def adapter(self, **kwargs: Any) -> Any:
        from backoffice.connectors.portals import EdpPortal

        return EdpPortal(client=self.client(), **kwargs)

    # ----------------------------------------------------------------- what tests look at

    def signed_in(self) -> int:
        """Sign-ins that got past the password (codes included)."""
        return sum(1 for r in self.requests if r.method == "POST" and r.url.path == "/area-cliente/entrar"
                   and _form(r).get("password") == PASSWORD)

    def downloads(self) -> list[str]:
        return [r.url.path.split("/")[3] for r in self.requests if r.url.path.endswith("/pdf")]

    def expire_sessions(self) -> None:
        for s in self.sessions.values():
            s["in"] = False

    # ----------------------------------------------------------------- the website

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host != HOST or request.url.scheme != "https":
            return httpx.Response(404, text="not this website")
        if self.down:
            return httpx.Response(503, text="Serviço temporariamente indisponível.")
        sid = _cookies(request).get(COOKIE, "")
        session = self.sessions.get(sid)
        path, method = request.url.path, request.method
        if path == "/area-cliente/entrar" and method == "GET":
            sid = secrets.token_hex(8)
            self.sessions[sid] = {"csrf": secrets.token_hex(8), "in": False}
            return self._html(page("entrar.html", csrf=self.sessions[sid]["csrf"], erro=""), sid)
        if path == "/area-cliente/entrar" and method == "POST":
            form = _form(request)
            self.passwords_seen.append((request.url.host, form.get("password", "")))
            if session is None or form.get("_csrf") != session["csrf"]:
                return self._sign_in_again(session, sid, "A sua sessão expirou. Tente novamente.")
            if self.accounts.get(form.get("email", "")) != form.get("password"):
                return self._sign_in_again(session, sid, "Email ou palavra-passe incorretos.")
            session["email"] = form["email"]
            if self.code:
                self.codes.append(f"{len(self.codes) + 1:06d}")
                session.update(code=self.codes[-1], code_at=self.clock(), pedido=secrets.token_hex(6))
                return self._html(page("codigo.html", pedido=session["pedido"], erro=""))
            return self._enter(sid, session)
        if path == "/area-cliente/codigo" and method == "POST":
            form = _form(request)
            if session is None or not session.get("code") or form.get("pedido") != session.get("pedido"):
                return self._html(page("codigo-expirado.html"))
            if self.clock() - session["code_at"] > timedelta(minutes=10):
                session["code"] = None
                return self._html(page("codigo-expirado.html"))
            if form.get("codigo") != session["code"]:
                return self._html(page("codigo.html", pedido=session["pedido"],
                                       erro='<p class="aviso erro">Código incorreto.</p>'))
            return self._enter(sid, session)
        if session is None or not session.get("in"):
            return httpx.Response(302, headers={"location": "/area-cliente/entrar"})
        if path == "/area-cliente/inicio":
            return self._html(page("inicio.html", nome="Ana"))
        if path == "/area-cliente/faturas":
            return self._list(int(request.url.params.get("ano") or 2026), int(request.url.params.get("pagina") or 1))
        if path.startswith("/area-cliente/faturas/") and path.endswith("/pdf"):
            if self.download_redirect:
                return httpx.Response(302, headers={"location": self.download_redirect})
            invoice = next((i for i in self.invoices if i.id == path.split("/")[3]), None)
            if invoice is None:
                return httpx.Response(404, text="Fatura não encontrada.")
            name = invoice.number.replace(" ", "_").replace("/", "_")
            return httpx.Response(200, content=invoice.pdf, headers={
                "content-type": "application/pdf", "content-disposition": f'attachment; filename="Fatura_{name}.pdf"'})
        return httpx.Response(404, text="Página não encontrada.")

    def _html(self, html: str, sid: str | None = None, status: int = 200) -> httpx.Response:
        headers = {"content-type": "text/html; charset=utf-8"}
        if sid:
            headers["set-cookie"] = f"{COOKIE}={sid}; Path=/; Secure; HttpOnly; SameSite=Lax"
        return httpx.Response(status, content=html.encode("utf-8"), headers=headers)

    def _sign_in_again(self, session: dict[str, Any] | None, sid: str, error: str) -> httpx.Response:
        if session is None:
            sid = secrets.token_hex(8)
            session = self.sessions[sid] = {"csrf": secrets.token_hex(8), "in": False}
        return self._html(page("entrar.html", csrf=session["csrf"], erro=f'<p class="aviso erro">{error}</p>'), sid)

    def _enter(self, sid: str, session: dict[str, Any]) -> httpx.Response:
        """Signed in: a new session id (the old one is dropped), then the customer area's home."""
        self.sessions.pop(sid, None)
        new = secrets.token_hex(8)
        self.sessions[new] = {"in": True, "email": session.get("email"), "csrf": secrets.token_hex(8)}
        return httpx.Response(302, headers={"location": "/area-cliente/inicio",
                                            "set-cookie": f"{COOKIE}={new}; Path=/; Secure; HttpOnly"})

    def _list(self, year: int, number: int) -> httpx.Response:
        if self.layout == "cards":
            return self._html(page("faturas-novo-layout.html"))
        mine = sorted((i for i in self.invoices if i.issued.year == year), key=lambda i: i.issued, reverse=True)
        shown = mine[(number - 1) * self.page_size: number * self.page_size]
        if not mine:
            lista = page("sem-faturas.html", ano=year)
        else:
            rows = "\n".join(page("linha.html", id=i.id, numero=i.number, data=f"{i.issued:%d/%m/%Y}",
                                  periodo=i.period, valor=i.total) for i in shown)
            lista = page("lista.html", linhas=rows)
        more = len(mine) > number * self.page_size
        nav = page("paginacao.html", ano=year, pagina=number, seguinte=number + 1) if more else ""
        return self._html(page("faturas.html", ano=year, lista=lista, paginacao=nav))


def _cookies(request: httpx.Request) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in request.headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name:
            out[name] = value
    return out


def _form(request: httpx.Request) -> dict[str, str]:
    if request.method != "POST":
        return {}
    return {k: v[0] for k, v in parse_qs(request.content.decode("utf-8"), keep_blank_values=True).items()}
