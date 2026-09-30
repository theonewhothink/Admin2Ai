-- 0008_pending_erasures: the sync worker completes account erasures (§52).
--
-- Erasing an account removes the business's data in one transaction and
-- leaves a record in tenant_erasures (0007). Its evidence files are removed
-- afterwards by the sync worker, in AWS as the evidence-deletion role (§25),
-- and the record is then marked purged under the tenant's own scope.
--
-- To find erasures whose files are still to purge, the worker lists them as
-- backoffice_scheduler (only after SET ROLE; its login holds the membership
-- WITH INHERIT FALSE). The scheduler sees just the id and the two dates of
-- such records: no other column, and no record that is already complete.

GRANT SELECT (tenant_id, completed_at, objects_purged_at) ON tenant_erasures TO backoffice_scheduler;

CREATE POLICY scheduler_lists_pending_erasures ON tenant_erasures FOR SELECT TO backoffice_scheduler
    USING (completed_at IS NOT NULL AND objects_purged_at IS NULL);
