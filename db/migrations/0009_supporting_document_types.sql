-- 0009_supporting_document_types: documents that are not accounting documents (§3, §50).
--
-- A pro-forma, a quote, a delivery or transport note (guia de remessa /
-- transporte), an order confirmation and a supplier's account statement
-- (extrato de conta corrente) are kept as supporting evidence only: they never
-- prove a purchase or a payment and are never booked. Each is its own kind,
-- mirrored from backoffice.domain.models.DocumentType.

ALTER DOMAIN document_type DROP CONSTRAINT document_type_check;
ALTER DOMAIN document_type ADD CONSTRAINT document_type_check
    CHECK (VALUE IN (
        'invoice', 'invoice_receipt', 'simplified_invoice', 'receipt', 'credit_note',
        'debit_note', 'statement', 'tax_notice', 'payroll', 'loan_statement',
        'contract', 'pro_forma', 'quote', 'delivery_note', 'order_confirmation',
        'supplier_statement', 'other'
    ));
