-- Platform -> codebase (Git repo) and observability link templates (Herald "fix it" shortcuts).
ALTER TABLE sources ADD COLUMN codebase jsonb, ADD COLUMN observability jsonb;
