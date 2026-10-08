Recorded pages imitating EDP's customer area (www.edp.pt/area-cliente), for the EDP adapter
(backoffice.invoice_sites.EDP, connectors/portals/invoice_pages.py). They were written by hand to follow the
structure of a customer area: a sign-in form with a hidden anti-forgery field, an SMS code step, the invoice list
per year with a PDF link per row, a "no invoices" notice and pagination. They are not copies of EDP's real pages,
and no real website is fetched by any test. A real EDP account is needed to confirm the addresses and selectors.

$placeholders are filled by the fake website in tests/_edp_site.py (string.Template).
