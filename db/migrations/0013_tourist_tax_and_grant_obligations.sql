-- 0013_tourist_tax_and_grant_obligations: local fees and grant letters become obligations (§24).
--
-- The municipal tourist tax ("taxa turística"): the monthly payment to the municipality, proven by that
-- payment, or its monthly declaration, proven by the submission receipt (checklist X26). Grant and subsidy
-- communications (IFAP, PEPAC, Portugal 2030 ...): documents to send by a deadline, proven by the agency's
-- acknowledgement or the owner's confirmation, and a grant payment announced, proven only by the money
-- arriving in the bank (checklist X30). Mirrors backoffice.domain.models.ObligationKind.

ALTER DOMAIN obligation_kind DROP CONSTRAINT obligation_kind_check;
ALTER DOMAIN obligation_kind ADD CONSTRAINT obligation_kind_check CHECK (VALUE IN (
    'tax_deadline', 'government_request', 'kyc_request', 'license_renewal',
    'insurance_renewal', 'contract_renewal', 'filing', 'rent', 'debt_collection',
    'bank_request', 'payment_deadline', 'tourist_tax', 'tourist_tax_declaration',
    'grant_documents', 'grant_payment'
));
