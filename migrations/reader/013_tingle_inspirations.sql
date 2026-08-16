-- Tingle inspiration stream.
-- Voice recordings remain canonical VoiceRecords. This table provides one
-- user-facing inspiration identity for Tingle voice and Hermes text entries.

CREATE TABLE IF NOT EXISTS reader.tingle_inspirations (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('voice', 'hermes_text')),
  voice_record_id TEXT UNIQUE REFERENCES reader.voice_records(id) ON DELETE CASCADE,
  external_ref TEXT,
  title TEXT NOT NULL DEFAULT '',
  content TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL,
  source_created_at TIMESTAMPTZ,
  archived_at TIMESTAMPTZ,
  purge_after TIMESTAMPTZ,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (
    (kind = 'voice' AND voice_record_id IS NOT NULL)
    OR (kind = 'hermes_text' AND voice_record_id IS NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reader_tingle_inspirations_external_ref
  ON reader.tingle_inspirations(external_ref)
  WHERE external_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_reader_tingle_inspirations_active
  ON reader.tingle_inspirations(
    (COALESCE(source_created_at, created_at)) DESC,
    id DESC
  )
  WHERE archived_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_reader_tingle_inspirations_purge
  ON reader.tingle_inspirations(purge_after)
  WHERE purge_after IS NOT NULL;

CREATE TABLE IF NOT EXISTS reader.tingle_tombstones (
  id TEXT PRIMARY KEY,
  inspiration_id TEXT NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK (kind IN ('voice', 'hermes_text')),
  voice_source TEXT,
  origin_ref TEXT,
  external_ref TEXT,
  device_id TEXT,
  client_capture_id TEXT,
  audio_hash TEXT,
  content_hash TEXT NOT NULL,
  deleted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_reader_tingle_tombstones_voice_origin
  ON reader.tingle_tombstones(voice_source, origin_ref)
  WHERE voice_source IS NOT NULL AND origin_ref IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_reader_tingle_tombstones_external_ref
  ON reader.tingle_tombstones(external_ref)
  WHERE external_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_reader_tingle_tombstones_audio_hash
  ON reader.tingle_tombstones(audio_hash)
  WHERE audio_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_reader_tingle_tombstones_client_capture
  ON reader.tingle_tombstones(client_capture_id)
  WHERE client_capture_id IS NOT NULL;

INSERT INTO reader.tingle_inspirations (
  id,
  kind,
  voice_record_id,
  content_hash,
  source_created_at,
  archived_at,
  metadata,
  created_at,
  updated_at
)
SELECT
  'tin_' || md5(vr.id),
  'voice',
  vr.id,
  md5(vr.id || ':' || vr.audio_hash),
  COALESCE(vr.recorded_at, vr.created_at),
  CASE WHEN vr.status = 'archived' THEN COALESCE(vr.archived_at, vr.updated_at) ELSE NULL END,
  jsonb_build_object(
    'schema', 'tingle.inspiration.v1',
    'created_by', '013_tingle_inspirations.sql'
  ),
  vr.created_at,
  vr.updated_at
FROM reader.voice_records vr
WHERE vr.source IN ('mac', 'click_mobile')
  AND COALESCE(vr.metadata->>'origin_kind', '') <> 'reader_audio_note'
  AND vr.deleted_at IS NULL
  AND NOT EXISTS (
    SELECT 1
    FROM reader.tingle_tombstones tombstone
    WHERE tombstone.kind = 'voice'
      AND tombstone.voice_source = vr.source
      AND tombstone.origin_ref = vr.origin_ref
  )
ON CONFLICT (voice_record_id) DO NOTHING;
