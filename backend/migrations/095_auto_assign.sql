-- AUTO-ASSIGN: who made the current assignment (actor dict, `system:auto-assign` for the automatic one) and
-- why (the decision model's reason; NULL for people). `assigned_by` set and `assignee_id` NULL means a person
-- took the alert back on purpose: the automatic assignment never runs again for it.
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS assigned_by jsonb;
ALTER TABLE alerts ADD COLUMN IF NOT EXISTS assign_reason text;
-- "fewest open assigned alerts in the team"
CREATE INDEX IF NOT EXISTS alerts_open_assignee ON alerts(team_id, assignee_id) WHERE status <> 'resolved';
