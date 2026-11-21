-- P5-PERF: the AI storm guard looks up suggestions created in the last couple of minutes.
CREATE INDEX IF NOT EXISTS suggestions_created ON suggestions(created_at DESC);
