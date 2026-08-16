-- Immutable Click Voice reading context provenance.

ALTER TABLE reader.voice_context_snapshots
  ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE reader.voice_context_snapshots
  ADD COLUMN IF NOT EXISTS created_by_run_id TEXT
    REFERENCES reader.voice_processing_runs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_reader_voice_context_snapshots_run
  ON reader.voice_context_snapshots(created_by_run_id)
  WHERE created_by_run_id IS NOT NULL;
