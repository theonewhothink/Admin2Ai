-- 0014_countries_managers_sensitive: a second country, outlet managers (§49, §51, §52).
--
-- Countries: a company runs on its own country's pack (Portugal, Spain). Spain's quarterly
-- VAT return (modelo 303) is an obligation its calendar sets, with no letter: a new kind,
-- added to every kind 0013 allows (the tourist tax and grant kinds stay). Mirrors
-- backoffice.domain.models.ObligationKind.
--
-- Managers: a "manager" membership is limited to one company (company_ids, exactly one) and to
-- one or more of its cost centers, such as a franchise's outlets (cost_center_ids). A manager
-- sees and answers only their outlets' questions, documents, payments and spending, and sends
-- receipts for them; never another outlet, another company, a bank connection or a setting
-- (server/auth.py MANAGER_ROUTES; the engine filters every read, service.BackOfficeService
-- .dispatch_manager). Sensitive documents (medical, legal, payslips and HR) are never shown to
-- them. Every read of a sensitive document's original is an event in the tenant's own
-- hash-chained log (who, when, which document), so nothing here stores it.

ALTER DOMAIN obligation_kind DROP CONSTRAINT obligation_kind_check;
ALTER DOMAIN obligation_kind ADD CONSTRAINT obligation_kind_check CHECK (VALUE IN (
    'tax_deadline', 'government_request', 'kyc_request', 'license_renewal',
    'insurance_renewal', 'contract_renewal', 'filing', 'rent', 'debt_collection',
    'bank_request', 'payment_deadline', 'tourist_tax', 'tourist_tax_declaration',
    'grant_documents', 'grant_payment', 'vat_return'
));

ALTER DOMAIN membership_role DROP CONSTRAINT membership_role_check;
ALTER DOMAIN membership_role ADD CONSTRAINT membership_role_check
    CHECK (VALUE IN ('owner', 'accountant', 'admin', 'employee', 'manager'));

ALTER TABLE memberships ADD COLUMN cost_center_ids text[] NOT NULL DEFAULT '{}';

-- company_ids: an accountant's companies (empty = every company), or a manager's one company.
ALTER TABLE memberships DROP CONSTRAINT memberships_company_ids_check;
ALTER TABLE memberships ADD CONSTRAINT memberships_company_ids_check
    CHECK (cardinality(company_ids) = 0
           OR (role IN ('accountant', 'manager') AND array_position(company_ids, NULL) IS NULL));
-- cost_center_ids: a manager's outlets, never empty, in exactly one company; nobody else has any.
ALTER TABLE memberships ADD CONSTRAINT memberships_cost_center_ids_check
    CHECK (CASE WHEN role = 'manager'
                THEN cardinality(cost_center_ids) > 0 AND cardinality(company_ids) = 1
                     AND array_position(cost_center_ids, NULL) IS NULL
                ELSE cardinality(cost_center_ids) = 0 END);

COMMENT ON COLUMN memberships.cost_center_ids IS
    'Manager memberships only: the cost centers (outlets) of their one company they may see and act for.';

GRANT UPDATE (cost_center_ids) ON memberships TO backoffice_app;
