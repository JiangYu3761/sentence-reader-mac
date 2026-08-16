-- Structured, persistent Click Voice discussion messages.

ALTER TABLE reader.voice_discussion_messages
  ADD COLUMN IF NOT EXISTS structured_content JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE reader.voice_discussion_messages
  ADD COLUMN IF NOT EXISTS adapter_version TEXT;

ALTER TABLE reader.voice_discussion_messages
  ADD COLUMN IF NOT EXISTS provider TEXT;

ALTER TABLE reader.voice_discussion_messages
  ADD COLUMN IF NOT EXISTS model TEXT;

ALTER TABLE reader.voice_discussion_messages
  ADD COLUMN IF NOT EXISTS client_message_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_reader_voice_discussion_messages_client
  ON reader.voice_discussion_messages(discussion_id, client_message_id)
  WHERE client_message_id IS NOT NULL;

ALTER TABLE reader.voice_discussions
  ADD COLUMN IF NOT EXISTS context_options JSONB NOT NULL DEFAULT '{}'::jsonb;
