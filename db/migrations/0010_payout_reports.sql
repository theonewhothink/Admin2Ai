-- 0010_payout_reports: payouts from card terminals and payment / sales platforms (§20, §21).
--
-- A payout into the bank from SIBS, REDUNIQ, Stripe, PayPal, Booking.com, Glovo,
-- Uber Eats, Airbnb, ... is the provider's net settlement of many sales: gross
-- sales minus its fees or commission, refunds and disputed payments. Its evidence
-- is the provider's payout report (models.DocumentType.PAYOUT_REPORT); a payout
-- the report proves to the cent is a 'payout_settlement' match
-- (reconciliation.engine.MatchKind.PAYOUT_SETTLEMENT). 'card_settlement' keeps
-- its meaning: paying off the business's own credit card.

ALTER DOMAIN document_type DROP CONSTRAINT document_type_check;
ALTER DOMAIN document_type ADD CONSTRAINT document_type_check CHECK (VALUE IN (
    'invoice', 'invoice_receipt', 'simplified_invoice', 'receipt', 'credit_note',
    'debit_note', 'statement', 'tax_notice', 'payroll', 'loan_statement',
    'contract', 'pro_forma', 'quote', 'delivery_note', 'order_confirmation',
    'supplier_statement', 'payout_report', 'other'
));

-- Mirrors backoffice.reconciliation.engine.MatchKind.
ALTER DOMAIN match_kind DROP CONSTRAINT match_kind_check;
ALTER DOMAIN match_kind ADD CONSTRAINT match_kind_check CHECK (VALUE IN (
    'one_to_one', 'one_payment_many_documents', 'many_payments_one_document',
    'partial_payment', 'card_settlement', 'payout_settlement'
));
