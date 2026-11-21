-- P3-RUNBOOK: lookups used by the step runner and the approvals inbox.
CREATE INDEX IF NOT EXISTS step_runs_approval ON step_runs(approval_id) WHERE approval_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS approvals_alert ON approvals(alert_id) WHERE alert_id IS NOT NULL;
