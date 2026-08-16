-- Click Android sync v2: monotonic change journal and idempotent operation receipts.
-- Safe to re-run. This migration does not delete or rewrite Reader user data.

CREATE TABLE IF NOT EXISTS reader.android_sync_resource_versions (
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  version BIGINT NOT NULL DEFAULT 1,
  deleted BOOLEAN NOT NULL DEFAULT false,
  last_sequence BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (resource_type, resource_id)
);

CREATE TABLE IF NOT EXISTS reader.android_sync_events (
  sequence BIGSERIAL PRIMARY KEY,
  resource_type TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  action TEXT NOT NULL CHECK (action IN ('insert', 'update', 'delete')),
  version BIGINT NOT NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  origin_device_id TEXT NOT NULL DEFAULT '',
  operation_id TEXT NOT NULL DEFAULT '',
  committed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reader_android_sync_events_resource
  ON reader.android_sync_events(resource_type, resource_id, sequence);

CREATE TABLE IF NOT EXISTS reader.android_sync_operation_receipts (
  operation_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('applied', 'conflict', 'permanent_failed')),
  http_status INTEGER NOT NULL DEFAULT 200,
  result JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_reader_android_sync_operation_receipts_device
  ON reader.android_sync_operation_receipts(device_id, updated_at);

CREATE TABLE IF NOT EXISTS reader.android_reading_position_checkpoints (
  book_id TEXT NOT NULL REFERENCES reader.books(id) ON DELETE CASCADE,
  device_id TEXT NOT NULL,
  chapter_id TEXT REFERENCES reader.chapters(id) ON DELETE SET NULL,
  chapter_locator TEXT NOT NULL,
  page_index INTEGER NOT NULL DEFAULT 0,
  total_pages INTEGER NOT NULL DEFAULT 1,
  page_ratio DOUBLE PRECISION NOT NULL DEFAULT 0,
  locator JSONB NOT NULL DEFAULT '{}'::jsonb,
  client_updated_at TIMESTAMPTZ,
  operation_id TEXT NOT NULL DEFAULT '',
  server_updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (book_id, device_id)
);

CREATE INDEX IF NOT EXISTS idx_reader_android_position_checkpoints_recent
  ON reader.android_reading_position_checkpoints(book_id, server_updated_at DESC);

-- Existing Reader rows become the version-1 full-sync baseline. Backfill does
-- not emit events; the first later mutation correctly advances them to v2.
INSERT INTO reader.android_sync_resource_versions (resource_type, resource_id, version, deleted, last_sequence)
SELECT 'book', id, 1, false, 0 FROM reader.books
ON CONFLICT (resource_type, resource_id) DO NOTHING;

INSERT INTO reader.android_sync_resource_versions (resource_type, resource_id, version, deleted, last_sequence)
SELECT 'library_state', book_id, 1, false, 0 FROM reader.library_state
ON CONFLICT (resource_type, resource_id) DO NOTHING;

INSERT INTO reader.android_sync_resource_versions (resource_type, resource_id, version, deleted, last_sequence)
SELECT DISTINCT 'book_asset', book_id, 1, false, 0 FROM reader.book_files
ON CONFLICT (resource_type, resource_id) DO NOTHING;

INSERT INTO reader.android_sync_resource_versions (resource_type, resource_id, version, deleted, last_sequence)
SELECT 'position', book_id, 1, false, 0 FROM reader.reading_positions
ON CONFLICT (resource_type, resource_id) DO NOTHING;

INSERT INTO reader.android_sync_resource_versions (resource_type, resource_id, version, deleted, last_sequence)
SELECT 'annotation', id, 1, false, 0 FROM reader.annotations
ON CONFLICT (resource_type, resource_id) DO NOTHING;

INSERT INTO reader.android_sync_resource_versions (resource_type, resource_id, version, deleted, last_sequence)
SELECT 'audio_note', id, 1, false, 0 FROM reader.audio_notes
ON CONFLICT (resource_type, resource_id) DO NOTHING;

CREATE OR REPLACE FUNCTION reader.capture_android_sync_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  row_payload JSONB;
  safe_payload JSONB;
  resource_type_value TEXT;
  resource_id_value TEXT;
  resource_version BIGINT;
  event_sequence BIGINT;
  deleted_value BOOLEAN;
BEGIN
  row_payload := CASE WHEN TG_OP = 'DELETE' THEN to_jsonb(OLD) ELSE to_jsonb(NEW) END;
  resource_type_value := TG_ARGV[0];
  resource_id_value := COALESCE(row_payload ->> TG_ARGV[1], '');
  IF resource_id_value = '' THEN
    RAISE EXCEPTION 'Android sync event missing resource id for %.%', TG_TABLE_SCHEMA, TG_TABLE_NAME;
  END IF;

  -- BIGSERIAL allocates numbers before commit. Serialize journal writers so a
  -- later sequence can never commit and be observed before an earlier one.
  PERFORM pg_advisory_xact_lock(hashtext('reader.android_sync_events')::bigint);

  deleted_value := TG_OP = 'DELETE';
  INSERT INTO reader.android_sync_resource_versions (
    resource_type, resource_id, version, deleted, updated_at
  )
  VALUES (resource_type_value, resource_id_value, 1, deleted_value, now())
  ON CONFLICT (resource_type, resource_id) DO UPDATE
  SET version = reader.android_sync_resource_versions.version + 1,
      deleted = EXCLUDED.deleted,
      updated_at = now()
  RETURNING version INTO resource_version;

  -- The journal is an ordering/index layer. Current safe projections are
  -- assembled by the API, so no book text, note text, transcript or Mac path
  -- belongs in the durable event itself (including tombstones).
  safe_payload := '{}'::jsonb;

  INSERT INTO reader.android_sync_events (
    resource_type,
    resource_id,
    action,
    version,
    payload,
    origin_device_id,
    operation_id
  )
  VALUES (
    resource_type_value,
    resource_id_value,
    lower(TG_OP),
    resource_version,
    COALESCE(safe_payload, '{}'::jsonb),
    COALESCE(current_setting('click.android_device_id', true), ''),
    COALESCE(current_setting('click.android_operation_id', true), '')
  )
  RETURNING sequence INTO event_sequence;

  UPDATE reader.android_sync_resource_versions
  SET last_sequence = event_sequence,
      updated_at = now()
  WHERE resource_type = resource_type_value
    AND resource_id = resource_id_value;

  IF TG_OP = 'DELETE' THEN
    RETURN OLD;
  END IF;
  RETURN NEW;
END;
$$;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'reader.books'::regclass AND tgname = 'android_sync_books' AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER android_sync_books
    AFTER INSERT OR UPDATE OR DELETE ON reader.books
    FOR EACH ROW EXECUTE FUNCTION reader.capture_android_sync_event('book', 'id', 'default');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'reader.library_state'::regclass AND tgname = 'android_sync_library_state' AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER android_sync_library_state
    AFTER INSERT OR UPDATE OR DELETE ON reader.library_state
    FOR EACH ROW EXECUTE FUNCTION reader.capture_android_sync_event('library_state', 'book_id', 'default');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'reader.book_files'::regclass AND tgname = 'android_sync_book_files' AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER android_sync_book_files
    AFTER INSERT OR UPDATE OR DELETE ON reader.book_files
    FOR EACH ROW EXECUTE FUNCTION reader.capture_android_sync_event('book_asset', 'book_id', 'book_asset');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'reader.reading_positions'::regclass AND tgname = 'android_sync_positions' AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER android_sync_positions
    AFTER INSERT OR UPDATE OR DELETE ON reader.reading_positions
    FOR EACH ROW EXECUTE FUNCTION reader.capture_android_sync_event('position', 'book_id', 'default');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'reader.annotations'::regclass AND tgname = 'android_sync_annotations' AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER android_sync_annotations
    AFTER INSERT OR UPDATE OR DELETE ON reader.annotations
    FOR EACH ROW EXECUTE FUNCTION reader.capture_android_sync_event('annotation', 'id', 'annotation');
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_trigger
    WHERE tgrelid = 'reader.audio_notes'::regclass AND tgname = 'android_sync_audio_notes' AND NOT tgisinternal
  ) THEN
    CREATE TRIGGER android_sync_audio_notes
    AFTER INSERT OR UPDATE OR DELETE ON reader.audio_notes
    FOR EACH ROW EXECUTE FUNCTION reader.capture_android_sync_event('audio_note', 'id', 'audio_note');
  END IF;
END;
$$;
