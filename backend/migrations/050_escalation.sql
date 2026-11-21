-- P3-ESCAL: idempotent shift materialisation and a short numeric id for approvals ("Reply APPROVE 12").
CREATE UNIQUE INDEX IF NOT EXISTS shifts_rotation_slot ON shifts(rotation_id, start_at)
    WHERE rotation_id IS NOT NULL;

ALTER TABLE approvals ADD COLUMN IF NOT EXISTS short_id bigserial;
CREATE UNIQUE INDEX IF NOT EXISTS approvals_short_id ON approvals(short_id);
