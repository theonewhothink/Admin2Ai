"""Portugal's words and rules live in the Portugal pack, not in the core (§49-50).

The core (every module outside ``backoffice/countries``) holds English and the languages no pack covers. Portugal's
own words ("fatura", "fecho de caixa", "novo IBAN", "setembro" ...) are in ``countries/pt/vocabulary.py`` and its
letters to suppliers in ``countries/pt/letters.py``; the core asks the registry for them
(``backoffice.countries.pack_words``, ``CompanyPack.vocabulary()``, ``CompanyPack.supplier_letters()``).

The guard scans the core's string literals (docstrings are documentation, not rules) for words only Portuguese uses.
Words Portuguese shares with Spanish ("nif", "iva", "recibo", "factura", "numero") are not on its list: the core reads
Spanish, which has no pack wording of its own yet.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

from backoffice.countries import company_pack, pack_words

SRC = Path(__file__).resolve().parents[1] / "src"

# Folded (lower case, no accents), whole words. Each is Portuguese only: Spanish and English spell it differently.
PORTUGUESE_WORDS = (
    "fatura", "faturas", "faturacao", "contribuinte", "nipc", "atcud", "multibanco", "mbway", "dinheiro", "caucao",
    "retencao", "devolucao", "cobranca", "contestacao", "relatorio", "fecho", "orcamento", "encomenda", "remessa",
    "morada", "levantamento", "despesa", "despesas", "emprestimo", "prestacao", "juros", "comissao", "imposto",
    "impostos", "financas", "vencimento", "utilizador", "telemovel", "cartao", "pagamento", "pagamentos", "janeiro",
    "fevereiro", "maio", "junho", "julho", "setembro", "outubro", "novembro", "dezembro", "talao", "taloes", "aluguer",
    "mensalidade", "extrato", "extratos", "ligacao", "sessao", "subscricao", "aplicacao", "condicoes", "descarregar",
    "obrigado", "obrigada", "nao", "tambem", "voce", "escreveu", "enviei", "relembramos", "cumprimentos", "poderiam",
    "senhorio", "arrendamento", "seguradora", "apolice", "coima", "penhora",
    "seguranca social", "autoridade tributaria", "conta corrente", "guia de remessa", "dados bancarios",
    "palavra-passe", "fecho de caixa", "recibo verde", "nota de encomenda", "fundo de maneio",
)  # fmt: skip
_WORD = re.compile(r"(?<![a-z])(?:" + "|".join(sorted(map(re.escape, PORTUGUESE_WORDS), key=len, reverse=True))
                   + r")(?![a-z])")
_LETTERS = re.compile(r"[ãõ]", re.IGNORECASE)  # letters only Portuguese writes ("não", "Configurações")

# Whole parts of the source that are data, not rules.
SKIPPED = {
    "countries": "the country packs themselves and the registry",
    "demo": "the demo business's sample documents: a Portuguese café's invoices and emails, data not rules",
    "acceptance.py": "the QA checklist: it names the spec's items (ATCUD, SAF-T) and the tests that prove them",
}
# Single strings that are genuinely not Portugal's wording, each with the reason.
ALLOWED = {
    ("reconciliation/payouts.py", "SIBS PAGAMENTOS"):
        "a company's registered name (Portugal's card acquirer), listed with STRIPE PAYMENTS EUROPE and the others",
    ("service.py", "Enter the API data from TOConline (Empresa > Configurações > Dados API)."):
        "the menu path in a Portuguese product's own screens (TOConline), quoted as the owner sees it",
}


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def portuguese_strings(source: str) -> list[tuple[int, str, str]]:
    """Every string literal in ``source`` (f-string parts included, docstrings not) with a Portuguese-only word or
    letter: (line, what was found, the string)."""
    tree = ast.parse(source)
    docstrings = _docstrings(tree)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            match = _WORD.search(_fold(node.value)) or _LETTERS.search(node.value)
            if match:
                found.append((node.lineno, match.group(0), node.value))
    return found


def test_the_scan_finds_portuguese_words_in_strings_and_not_in_docstrings_or_english() -> None:
    assert portuguese_strings('_TITLE = re.compile(r"relatorio\\s+z|z\\s+report")')
    assert portuguese_strings('x = f"Fatura {number}"')  # an f-string's text
    assert portuguese_strings('LINE = "Pagamento por MB WAY"')
    assert portuguese_strings('LABEL = "Configurações"')  # a letter only Portuguese writes
    assert portuguese_strings('WORDS = ("Fecho de Caixa", "z report")')
    assert not portuguese_strings('def f():\n    """Reads Portugal\'s "fecho de caixa" through the pack."""\n')
    assert not portuguese_strings('WORDS = ("z report", "factura", "recibo", "NIF", "IVA", "September")')


def test_no_core_module_holds_portuguese_words_outside_the_portugal_pack() -> None:
    offenders: dict[str, list[tuple[int, str, str]]] = {}
    used: set[tuple[str, str]] = set()
    scanned = 0
    for path in sorted((SRC / "backoffice").rglob("*.py")):
        relative = path.relative_to(SRC / "backoffice").as_posix()
        if relative.split("/")[0] in SKIPPED or relative in SKIPPED:
            continue
        scanned += 1
        for line, word, text in portuguese_strings(path.read_text(encoding="utf-8")):
            if (relative, text) in ALLOWED:
                used.add((relative, text))
            else:
                offenders.setdefault(relative, []).append((line, word, text[:120]))
    assert scanned > 100
    assert offenders == {}
    assert used == set(ALLOWED)  # an allowance nothing needs any more is removed


def test_portugals_words_come_from_its_pack_and_spain_adds_none() -> None:
    portugal, spain = company_pack("PT"), company_pack("ES")
    assert "relatorio\\s+(?:z|de\\s+fecho|diario\\s+de\\s+vendas)" in portugal.vocabulary()["tills.title"]
    assert portugal.vocabulary()["month:9"] == ("setembro",)
    assert dict(spain.vocabulary()) == {}
    assert pack_words("tills.title") == portugal.vocabulary()["tills.title"]
    assert portugal.supplier_letters().request("FT 2026/183", "117,20 €", "18 de setembro", "NIF 516123459",
                                               "Hazel Tree")[0] == "Fatura FT 2026/183"
    assert spain.supplier_letters() is None


_PROBE = """
import json, sys
from datetime import date
if sys.argv[1] == "without":
    from backoffice.countries.pt import PortugalPack
    PortugalPack.vocabulary = lambda self: {}
from backoffice.fraud.phrases import find_suspicious_phrases
from backoffice.learning import fold
from backoffice.tills import read_till_reports
from backoffice.understanding import find_periods

def read(email, till, chat):
    return {"fraud": sorted({h.category.value for h in find_suspicious_phrases(email)}),
            "till": [str(day.total) for day in read_till_reports(till)],
            "months": [p.label for p in find_periods(fold(chat), date(2026, 10, 8))[0]]}

print(json.dumps({
    "portuguese": read("Segue o nosso novo IBAN. Pagamento com urgência, de forma confidencial.",
                       "RELATÓRIO Z N.º 0918\\nData: 17/09/2026\\nNumerário: 225,00\\nMultibanco: 1.405,40\\n"
                       "Total de vendas: 1.630,40\\n",
                       "as faturas de setembro"),
    "english": read("Please pay our new bank details. Urgent and confidential.",
                    "Z REPORT #0412\\nDate: 21/09/2026\\nCash: 98.40\\nCard: 311.25\\nTotal: 409.65\\n",
                    "the invoices from September"),
}))
"""


def _read_with(pack_words_: str) -> dict:
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    out = subprocess.run([sys.executable, "-c", _PROBE, pack_words_], check=True, env=env, capture_output=True,
                         text=True)
    return json.loads(out.stdout)


def test_the_core_reads_portuguese_only_with_the_portugal_packs_words() -> None:
    # The same core readers (fraud phrases, till reports, the chat's months), in a fresh process each time.
    english = {"fraud": ["bank_change", "secrecy", "urgency"], "till": ["409.65"], "months": ["September"]}
    assert _read_with("with") == {
        "portuguese": {"fraud": ["bank_change", "secrecy", "urgency"], "till": ["1630.40"], "months": ["September"]},
        "english": english,
    }
    # Without the pack's words the core reads no Portuguese at all, and its English is untouched.
    assert _read_with("without") == {"portuguese": {"fraud": [], "till": [], "months": []}, "english": english}


def test_importing_the_core_readers_does_not_load_the_portugal_pack() -> None:
    code = ("import sys\n"
            "import backoffice.fraud.phrases, backoffice.tills, backoffice.understanding, backoffice.missing.chase\n"
            "import backoffice.assistant, backoffice.policy.privacy, backoffice.orchestrator\n"
            "assert 'backoffice.countries.pt' not in sys.modules\n")
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    subprocess.run([sys.executable, "-c", code], check=True, env=env)
