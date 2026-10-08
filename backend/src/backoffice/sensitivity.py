"""Sensitive documents and least exposure (§52, §53; checklist X32, cases 21 law firm and 46 pharmacy).

Some documents carry what nobody but the owner and the company's accountant should see:

* **medical**: patient or health data (a prescription, a patient record, a clinical report);
* **legal**: a legal matter (court papers, a client's case file, privileged correspondence);
* **hr**: pay and staff files (payslips, employment contracts, disciplinary or sick-leave papers).

A document is marked sensitive by its own wording (:func:`classify`, Portuguese, Spanish and English
phrases; a payslip always) or by the owner. A sensitive document is then:

* never sent to an external AI, even when external AI is switched on: the reader keeps it on our
  servers (:class:`backoffice.reading.ReadRequest` ``sensitive``), and the chat's model sees only its
  summary line (:func:`scrub`);
* hidden from employees and outlet managers; the accountant sees it only for their own companies;
* opened only on the record: every read of its original (who, when, which document) goes to the
  access log the owner reads (``GET /api/documents/access-log``).

The phrase tables are conventions (verified_as_of: never); they err on the side of marking a
document sensitive, which only narrows who sees it.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from backoffice.countries import LazyPattern, pack_words
from backoffice.domain.models import DocumentType

__all__ = ["CATEGORY_WORDS", "KIND_WORDS", "classify", "scrub", "summary_line"]

# Owner-facing words for why a document is sensitive (§36).
CATEGORY_WORDS: Mapping[str, str] = {
    "medical": "It has medical or patient information.",
    "legal": "It is about a legal matter.",
    "hr": "It has pay or staff information.",
    "owner": "You marked it sensitive.",
}
KIND_WORDS: Mapping[str, str] = {
    "medical": "Medical document", "legal": "Legal document", "hr": "Staff document", "owner": "Sensitive document",
}

# Folded phrases (lower case, no accents). Multi-word on purpose: a single common word ("court",
# "patient") appears on ordinary invoices too. A pack adds its own ("sensitivity.<category>": Portugal's
# "receita médica", "processo judicial", "recibo de vencimento").
_PHRASES: Mapping[str, tuple[str, ...]] = {
    "medical": (
        # Spanish
        "receta medica", "informe medico", "datos de salud", "tarjeta sanitaria", "numero de paciente",
        "nombre del paciente", "diagnostico medico",
        # English
        "patient name", "patient id", "patient number", "patient record", "medical record", "medical report",
        "prescription for", "clinical notes", "diagnosis", "health record", "date of birth and nhs",
    ),
    "legal": (
        # Spanish
        "procedimiento judicial", "juzgado de", "demanda judicial", "expediente judicial", "secreto profesional",
        "auto judicial", "sentencia n", "procurador de los tribunales",
        # English
        "privileged and confidential", "legally privileged", "attorney-client", "attorney client privilege",
        "court case", "high court", "court of appeal", "statement of claim",
        "legal proceedings",
    ),
    "hr": (
        # Spanish
        "recibo de salarios", "hoja de salario", "nomina de", "nomina mensual", "contrato de trabajo",
        "evaluacion del desempeno", "expediente disciplinario", "baja medica", "carta de despido",
        # English
        "payslip", "pay slip", "salary slip", "pay stub", "payroll statement", "employment contract",
        "performance review", "disciplinary", "sick leave", "sick note", "dismissal letter",
    ),
}


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _pattern(phrases: Iterable[str]) -> re.Pattern[str]:
    body = "|".join(re.escape(_fold(p)).replace(r"\ ", r"\s+") for p in sorted(set(phrases), key=len, reverse=True))
    return re.compile(rf"(?<![0-9a-z])(?:{body})(?![a-z])")


_PATTERNS = {category: LazyPattern(lambda c=category: _pattern((*_PHRASES[c], *pack_words(f"sensitivity.{c}"))).pattern)
             for category in _PHRASES}


def classify(*texts: str | None, doc_type: DocumentType | None = None) -> str | None:
    """"medical", "legal" or "hr" when the document's own words (or kind: a payslip) say it is sensitive;
    None otherwise. Several texts are read together (the document, the email around it, its file name)."""
    if doc_type is DocumentType.PAYROLL:
        return "hr"
    folded = _fold("\n".join(t for t in texts if t))
    if not folded.strip():
        return None
    for category in ("medical", "legal", "hr"):
        if _PATTERNS[category].search(folded):
            return category
    return None


def summary_line(category: str | None, *, date_text: str = "", company: str = "") -> str:
    """The one line the chat and other narrow views may show of a sensitive document: its kind, its date and
    its company, never its content, its people or its amounts."""
    kind = KIND_WORDS.get(category or "", "Sensitive document")
    parts = [kind, *(p for p in (date_text, company) if p)]
    return " · ".join(parts) + " (sensitive: details are not shown here)"


_ID_KEYS = ("id", "documentId", "document_id", "doc_id")
_EVIDENCE_KEYS = ("evidenceIds", "evidence_ids")


def scrub(value: Any, *, documents: Mapping[str, str], evidence: Mapping[str, str]) -> Any:
    """``value`` (a tool result, a view) with every record of a sensitive document replaced by its summary line.

    ``documents`` maps sensitive document ids to their summary lines; ``evidence`` maps the evidence ids of
    those documents to the same lines. A dict that names one of them (by id, document id, or its evidence)
    becomes ``{"id", "summary", "sensitive": True}``; an evidence chip pointing at one becomes its line.
    """
    if not documents and not evidence:
        return value
    if isinstance(value, Mapping):
        for key in _ID_KEYS:
            found = value.get(key)
            if isinstance(found, str) and found in documents:
                return {"id": found, "summary": documents[found], "sensitive": True}
        for key in _EVIDENCE_KEYS:
            ids = value.get(key)
            if isinstance(ids, (list, tuple)) and any(isinstance(i, str) and i in evidence for i in ids):
                line = next(evidence[i] for i in ids if isinstance(i, str) and i in evidence)
                return {"id": value.get("id"), "summary": line, "sensitive": True}
        found = value.get("id")
        if isinstance(found, str) and found in evidence:
            return {"id": found, "summary": evidence[found], "sensitive": True}
        return {k: scrub(v, documents=documents, evidence=evidence) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub(v, documents=documents, evidence=evidence) for v in value]
    return value


def mentions(value: Any, ids: Collection[str]) -> bool:
    """True when ``value`` (a JSON-able view) names one of ``ids`` anywhere."""
    if isinstance(value, str):
        return value in ids
    if isinstance(value, Mapping):
        return any(mentions(v, ids) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(mentions(v, ids) for v in value)
    return False
