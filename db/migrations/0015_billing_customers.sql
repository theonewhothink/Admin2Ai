-- 0015_billing_customers: which business a payment-provider customer pays for (§60, §61).
--
-- Plans are bought on the payment provider's own pages (Stripe Checkout and its customer
-- portal): card details never reach the back office. The provider reports what happened
-- through signed webhooks, which carry no tenant scope. Checkout puts the business's id on
-- the session and the subscription; this table is the fallback for everything else (a
-- failed invoice payment names only its customer): the first event that names a customer
-- links it to the business, in the same transaction as that event (server/store.py
-- IndexOp "link_billing_customer").
--
-- The plan itself, the provider's events already applied and what waits for the plan live
-- in the business's own hash-chained event log (backoffice.billing), not here.
--
-- Row-level security: a business reads and links its own customers (app.tenant_id); a
-- webhook finds the one customer it presents (app.billing_customer). With neither set,
-- nothing is visible. Erasing the business removes its rows (ON DELETE CASCADE).

CREATE TABLE billing_customers (
    customer_id text        PRIMARY KEY CHECK (customer_id ~ '^[A-Za-z0-9_]{3,255}$'),
    tenant_id   tenant_key  NOT NULL REFERENCES tenants (id) ON DELETE CASCADE,
    linked_at   timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE billing_customers IS
    'Which business a payment-provider customer pays for (§61). Found by the customer id a webhook presents (app.billing_customer).';

CREATE INDEX billing_customers_tenant_idx ON billing_customers (tenant_id);

CALL enable_tenant_isolation('billing_customers');
CREATE POLICY presented_customer ON billing_customers FOR SELECT
    USING (customer_id = current_setting('app.billing_customer', true));

GRANT SELECT, INSERT ON billing_customers TO backoffice_app;
GRANT SELECT ON billing_customers TO backoffice_readonly;
