"""Portuguese suppliers' invoice websites (backoffice.invoice_sites): where each keeps its sign-in and invoices."""

from __future__ import annotations

from backoffice.invoice_sites import InvoicePageSite

# EDP (electricity and gas, Portugal): the customer area's sign-in, its SMS code and the invoice list per year.
# Written against recorded pages that imitate the area's structure; not yet checked with a real EDP account.
EDP = InvoicePageSite(
    key="edp_pt", name="EDP", hosts=("edp.pt",), names=("EDP", "EDP Comercial", "EDP Energia"),
    sign_in_url="https://www.edp.pt/area-cliente/entrar",
    sign_in_form="form#form-login", username_field="email", password_field="password",
    signed_in="main[data-area-cliente]",
    code_form="form#form-codigo", code_field="codigo", code_channel="sms", code_expired=".codigo-expirado",
    invoices_url="https://www.edp.pt/area-cliente/faturas?ano={year}",
    invoice_row="table.lista-faturas > tbody > tr", row_id="data-fatura-id",
    number="td.numero", issue_date="td.data-emissao", amount="td.valor", download="a.descarregar-pdf",
    period="td.periodo", no_invoices=".sem-faturas", next_page="nav.paginacao a[rel=next]",
)

SITES: tuple[InvoicePageSite, ...] = (EDP,)
