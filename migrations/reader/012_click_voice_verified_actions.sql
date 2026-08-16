-- Verified Click Voice action settlement.
-- Existing actions and target data are preserved. External actions only become
-- validated after a target write and an independent read-back receipt.

ALTER TABLE reader.voice_actions
  DROP CONSTRAINT IF EXISTS voice_actions_status_check;

ALTER TABLE reader.voice_actions
  ADD CONSTRAINT voice_actions_status_check
  CHECK (
    status IN (
      'proposed',
      'pending_user_confirmation',
      'applying',
      'applied',
      'validating',
      'auto_applied',
      'validated',
      'rejected',
      'superseded',
      'failed_apply'
    )
  );

ALTER TABLE reader.voice_actions
  ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;

ALTER TABLE reader.voice_actions
  ADD COLUMN IF NOT EXISTS confirmed_by TEXT;

ALTER TABLE reader.voice_actions
  ADD COLUMN IF NOT EXISTS target_adapter TEXT;

ALTER TABLE reader.voice_actions
  ADD COLUMN IF NOT EXISTS target_identity JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE reader.voice_actions
  ADD COLUMN IF NOT EXISTS validation_receipt JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE reader.voice_actions
  ADD COLUMN IF NOT EXISTS apply_job_id TEXT
    REFERENCES reader.voice_processing_jobs(id) ON DELETE SET NULL;

ALTER TABLE reader.voice_actions
  ADD COLUMN IF NOT EXISTS validate_job_id TEXT
    REFERENCES reader.voice_processing_jobs(id) ON DELETE SET NULL;

CREATE TABLE IF NOT EXISTS reader.voice_action_receipts (
  id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL REFERENCES reader.voice_actions(id) ON DELETE CASCADE,
  voice_record_id TEXT NOT NULL REFERENCES reader.voice_records(id) ON DELETE CASCADE,
  processing_run_id TEXT REFERENCES reader.voice_processing_runs(id) ON DELETE SET NULL,
  phase TEXT NOT NULL CHECK (phase IN ('apply', 'validate')),
  target_adapter TEXT NOT NULL,
  target_identity JSONB NOT NULL DEFAULT '{}'::jsonb,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
  receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
  failure_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reader_voice_action_receipts_action
  ON reader.voice_action_receipts(action_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reader_voice_action_receipts_record
  ON reader.voice_action_receipts(voice_record_id, created_at DESC);

CREATE TABLE IF NOT EXISTS reader.voice_record_links (
  id TEXT PRIMARY KEY,
  voice_record_id TEXT NOT NULL REFERENCES reader.voice_records(id) ON DELETE CASCADE,
  action_id TEXT REFERENCES reader.voice_actions(id) ON DELETE SET NULL,
  target_type TEXT NOT NULL CHECK (target_type IN ('book', 'annotation', 'knowledge_base_note')),
  target_id TEXT NOT NULL,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (voice_record_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_reader_voice_record_links_record
  ON reader.voice_record_links(voice_record_id, created_at DESC);
