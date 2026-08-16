-- Versioned Click Voice Hermes processing and action provenance.
-- Existing actions are preserved; only the allowed lifecycle is extended.

ALTER TABLE reader.voice_actions
  DROP CONSTRAINT IF EXISTS voice_actions_status_check;

ALTER TABLE reader.voice_actions
  ADD CONSTRAINT voice_actions_status_check
  CHECK (
    status IN (
      'proposed',
      'pending_user_confirmation',
      'auto_applied',
      'applied',
      'rejected',
      'superseded',
      'failed_apply',
      'validated'
    )
  );

ALTER TABLE reader.voice_actions
  ADD COLUMN IF NOT EXISTS proposed_by_run_id TEXT
    REFERENCES reader.voice_processing_runs(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_reader_voice_actions_proposed_by_run
  ON reader.voice_actions(proposed_by_run_id)
  WHERE proposed_by_run_id IS NOT NULL;
