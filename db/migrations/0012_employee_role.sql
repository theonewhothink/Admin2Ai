-- 0012_employee_role: employees who hold company cards or pay expenses themselves (backoffice.staff).
--
-- An employee membership lets a cardholder sign in to send receipts: they may read only their
-- own open card payments and upload receipts (and expense claims for what they paid with their own
-- money). Every other route refuses them (server/auth.py EMPLOYEE_ROUTES); nothing in the schema
-- is readable more widely than for any other member.

ALTER DOMAIN membership_role DROP CONSTRAINT membership_role_check;
ALTER DOMAIN membership_role ADD CONSTRAINT membership_role_check
    CHECK (VALUE IN ('owner', 'accountant', 'admin', 'employee'));
