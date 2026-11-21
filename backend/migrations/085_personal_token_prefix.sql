-- P4-MCP: display prefix of a personal access token (the token itself is only stored as a hash).
ALTER TABLE personal_tokens ADD COLUMN IF NOT EXISTS prefix text NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS personal_tokens_user ON personal_tokens(user_id) WHERE revoked_at IS NULL;
