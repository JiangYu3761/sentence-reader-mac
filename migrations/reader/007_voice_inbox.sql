-- Click Voice Inbox V1.
-- Safe to re-run: creates a PostgreSQL primary store for cross-device voice assets
-- without deleting or replacing reader.audio_notes or the legacy Recordings SQLite index.

CREATE TABLE IF NOT EXISTS reader.voice_records (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  source TEXT NOT NULL
    CHECK (source IN ('mac', 'click_mobile', 'import', 'platform_forward', 'browser', 'future')),
  source_platform TEXT,
  device_id TEXT,
  origin_ref TEXT,
  audio_uri TEXT NOT NULL,
  audio_hash TEXT NOT NULL,
  duration_ms BIGINT,
  mime_type TEXT,
  language TEXT NOT NULL DEFAULT 'zh',
  transcript TEXT,
  transcript_confidence DOUBLE PRECISION,
  summary TEXT,
  intent_type TEXT NOT NULL DEFAULT 'unknown'
    CHECK (intent_type IN ('task', 'idea', 'question', 'review', 'reading_note', 'meeting_note', 'material', 'memo', 'unknown')),
  priority INTEGER NOT NULL DEFAULT 0,
  project_hint TEXT,
  evidence_spans JSONB NOT NULL DEFAULT '[]'::jsonb,
  hermes_result_id TEXT,
  latest_processing_run_id TEXT,
  status TEXT NOT NULL
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
        'failed_understand',
        'failed_action',
        'deleted'
      )
    ),
  failure_code TEXT,
  failure_message TEXT,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  recorded_at TIMESTAMPTZ,
  uploaded_at TIMESTAMPTZ,
  transcribed_at TIMESTAMPTZ,
  processed_at TIMESTAMPTZ,
  archived_at TIMESTAMPTZ,
  deleted_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reader_voice_records_source_origin
  ON reader.voice_records(source, origin_ref)
  WHERE origin_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_reader_voice_records_status
  ON reader.voice_records(status, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_reader_voice_records_source
  ON reader.voice_records(source, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reader_voice_records_not_deleted
  ON reader.voice_records(updated_at DESC)
  WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS reader.voice_processing_runs (
  id TEXT PRIMARY KEY,
  voice_record_id TEXT NOT NULL REFERENCES reader.voice_records(id) ON DELETE CASCADE,
  run_type TEXT NOT NULL
    CHECK (run_type IN ('upload', 'transcribe', 'understand', 'title_summary', 'action_extract', 'apply_action', 'validate')),
  provider TEXT NOT NULL,
  called_hermes BOOLEAN NOT NULL DEFAULT false,
  model TEXT,
  input_hash TEXT,
  prompt_hash TEXT,
  output_hash TEXT,
  status TEXT NOT NULL CHECK (status IN ('success', 'failed', 'skipped')),
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at TIMESTAMPTZ,
  duration_seconds DOUBLE PRECISION,
  fallback_used BOOLEAN NOT NULL DEFAULT false,
  failure_reason TEXT,
  request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  response_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  request_payload_uri TEXT,
  response_payload_uri TEXT,
  created_actions JSONB NOT NULL DEFAULT '[]'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reader_voice_processing_runs_record
  ON reader.voice_processing_runs(voice_record_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reader_voice_processing_runs_status
  ON reader.voice_processing_runs(status, created_at DESC);

CREATE TABLE IF NOT EXISTS reader.voice_actions (
  id TEXT PRIMARY KEY,
  voice_record_id TEXT NOT NULL REFERENCES reader.voice_records(id) ON DELETE CASCADE,
  action_type TEXT NOT NULL
    CHECK (
      action_type IN (
        'create_note',
        'create_task',
        'append_reading_note',
        'create_review_item',
        'add_to_knowledge_base',
        'ask_followup',
        'archive',
        'no_action'
      )
    ),
  title TEXT NOT NULL,
  body TEXT NOT NULL DEFAULT '',
  target JSONB NOT NULL DEFAULT '{}'::jsonb,
  risk TEXT NOT NULL DEFAULT 'medium' CHECK (risk IN ('low', 'medium', 'high')),
  status TEXT NOT NULL
    CHECK (
      status IN (
        'proposed',
        'pending_user_confirmation',
        'auto_applied',
        'applied',
        'rejected',
        'failed_apply',
        'validated'
      )
    ),
  evidence_spans JSONB NOT NULL DEFAULT '[]'::jsonb,
  requires_confirmation BOOLEAN NOT NULL DEFAULT true,
  applied_result JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  applied_at TIMESTAMPTZ,
  validated_at TIMESTAMPTZ,
  failure_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_reader_voice_actions_record
  ON reader.voice_actions(voice_record_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_reader_voice_actions_status
  ON reader.voice_actions(status, created_at DESC);
