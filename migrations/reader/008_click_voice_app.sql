-- Click Voice app contracts.
-- This migration only adds recording-specific state. It preserves existing
-- VoiceRecord rows, original audio references, and legacy transcript content.

ALTER TABLE reader.voice_records
  DROP CONSTRAINT IF EXISTS voice_records_status_check;

ALTER TABLE reader.voice_records
  ADD CONSTRAINT voice_records_status_check
  CHECK (
    status IN (
      'recording',
      'saved_local',
      'upload_pending',
      'uploading',
      'uploaded',
      'transcribe_pending',
      'transcribing',
      'transcribed',
      'cleanup_pending',
      'cleaning',
      'cleaned',
      'understand_pending',
      'understanding',
      'needs_user_confirmation',
      'needs_followup',
      'action_pending',
      'action_applied',
      'settled',
      'archived',
      'failed_upload',
      'failed_transcribe',
      'failed_cleanup',
      'failed_understand',
      'failed_action',
      'deleted'
    )
  );

ALTER TABLE reader.voice_processing_runs
  DROP CONSTRAINT IF EXISTS voice_processing_runs_run_type_check;

ALTER TABLE reader.voice_processing_runs
  ADD CONSTRAINT voice_processing_runs_run_type_check
  CHECK (
    run_type IN (
      'upload',
      'transcribe',
      'clean_transcript',
      'understand',
      'build_context',
      'discuss',
      'title_summary',
      'action_extract',
      'apply_action',
      'validate'
    )
  );

CREATE TABLE IF NOT EXISTS reader.voice_transcript_versions (
  id TEXT PRIMARY KEY,
  voice_record_id TEXT NOT NULL REFERENCES reader.voice_records(id) ON DELETE CASCADE,
  version_type TEXT NOT NULL
    CHECK (version_type IN ('asr_raw', 'hermes_cleaned', 'user_edited')),
  parent_version_id TEXT REFERENCES reader.voice_transcript_versions(id) ON DELETE SET NULL,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  provider TEXT NOT NULL,
  processing_run_id TEXT REFERENCES reader.voice_processing_runs(id) ON DELETE SET NULL,
  changes JSONB NOT NULL DEFAULT '[]'::jsonb,
  confidence DOUBLE PRECISION,
  meaning_changed BOOLEAN NOT NULL DEFAULT false,
  needs_review BOOLEAN NOT NULL DEFAULT false,
  is_active BOOLEAN NOT NULL DEFAULT false,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reader_voice_transcript_versions_record
  ON reader.voice_transcript_versions(voice_record_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reader_voice_transcript_versions_active
  ON reader.voice_transcript_versions(voice_record_id)
  WHERE is_active;

ALTER TABLE reader.voice_records
  ADD COLUMN IF NOT EXISTS active_transcript_version_id TEXT
    REFERENCES reader.voice_transcript_versions(id) ON DELETE SET NULL;

ALTER TABLE reader.voice_records
  ADD COLUMN IF NOT EXISTS cleaned_at TIMESTAMPTZ;

INSERT INTO reader.voice_transcript_versions (
  id,
  voice_record_id,
  version_type,
  content,
  content_hash,
  provider,
  confidence,
  is_active,
  metadata
)
SELECT
  'vtv_' || md5(vr.id || ':' || vr.transcript),
  vr.id,
  'asr_raw',
  vr.transcript,
  md5(vr.transcript),
  'legacy_voice_record',
  vr.transcript_confidence,
  vr.active_transcript_version_id IS NULL,
  jsonb_build_object('backfilled_by', '008_click_voice_app.sql')
FROM reader.voice_records vr
WHERE NULLIF(btrim(vr.transcript), '') IS NOT NULL
  AND NOT EXISTS (
    SELECT 1
    FROM reader.voice_transcript_versions existing
    WHERE existing.voice_record_id = vr.id
  )
ON CONFLICT (id) DO NOTHING;

UPDATE reader.voice_records vr
SET active_transcript_version_id = vtv.id
FROM reader.voice_transcript_versions vtv
WHERE vr.id = vtv.voice_record_id
  AND vtv.is_active
  AND vr.active_transcript_version_id IS NULL;

CREATE TABLE IF NOT EXISTS reader.voice_processing_jobs (
  id TEXT PRIMARY KEY,
  voice_record_id TEXT NOT NULL REFERENCES reader.voice_records(id) ON DELETE CASCADE,
  job_type TEXT NOT NULL
    CHECK (
      job_type IN (
        'transcribe',
        'clean_transcript',
        'understand',
        'build_context',
        'prepare_discussion',
        'apply_action',
        'validate_action'
      )
    ),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'cancelled')),
  priority INTEGER NOT NULL DEFAULT 0,
  attempt_count INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  input_hash TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  lease_owner TEXT,
  lease_expires_at TIMESTAMPTZ,
  processing_run_id TEXT REFERENCES reader.voice_processing_runs(id) ON DELETE SET NULL,
  last_error TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_reader_voice_processing_jobs_claim
  ON reader.voice_processing_jobs(status, available_at, priority DESC, created_at)
  WHERE status IN ('pending', 'running');

CREATE INDEX IF NOT EXISTS idx_reader_voice_processing_jobs_record
  ON reader.voice_processing_jobs(voice_record_id, created_at DESC);

CREATE TABLE IF NOT EXISTS reader.voice_context_snapshots (
  id TEXT PRIMARY KEY,
  voice_record_id TEXT NOT NULL REFERENCES reader.voice_records(id) ON DELETE CASCADE,
  scope TEXT NOT NULL
    CHECK (scope IN ('record_only', 'recent_reading', 'selected_books', 'recent_voice')),
  source_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
  evidence_items JSONB NOT NULL DEFAULT '[]'::jsonb,
  relevance_decisions JSONB NOT NULL DEFAULT '[]'::jsonb,
  input_hash TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reader_voice_context_snapshots_record
  ON reader.voice_context_snapshots(voice_record_id, created_at DESC);

CREATE TABLE IF NOT EXISTS reader.voice_discussions (
  id TEXT PRIMARY KEY,
  voice_record_id TEXT NOT NULL REFERENCES reader.voice_records(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  context_scope TEXT NOT NULL DEFAULT 'recent_reading'
    CHECK (context_scope IN ('record_only', 'recent_reading', 'selected_books', 'recent_voice')),
  current_context_snapshot_id TEXT REFERENCES reader.voice_context_snapshots(id) ON DELETE SET NULL,
  hermes_session_id TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reader_voice_discussions_record
  ON reader.voice_discussions(voice_record_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS reader.voice_discussion_messages (
  id TEXT PRIMARY KEY,
  discussion_id TEXT NOT NULL REFERENCES reader.voice_discussions(id) ON DELETE CASCADE,
  role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
  content TEXT NOT NULL,
  citations JSONB NOT NULL DEFAULT '[]'::jsonb,
  context_snapshot_id TEXT REFERENCES reader.voice_context_snapshots(id) ON DELETE SET NULL,
  processing_run_id TEXT REFERENCES reader.voice_processing_runs(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'succeeded' CHECK (status IN ('pending', 'succeeded', 'failed')),
  failure_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reader_voice_discussion_messages_discussion
  ON reader.voice_discussion_messages(discussion_id, created_at ASC);
