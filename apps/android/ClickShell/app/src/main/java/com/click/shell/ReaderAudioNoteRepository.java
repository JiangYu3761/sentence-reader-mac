package com.click.shell;

import android.content.Context;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.util.Locale;
import java.util.UUID;

/**
 * Owns durable reader-note audio under one app-private root.
 *
 * <p>Database metadata stores only a root-relative token plus size and SHA-256. Callers can never
 * resolve an arbitrary absolute path through this class.</p>
 */
final class ReaderAudioNoteRepository {
    static final String ROOT_NAME = "click-audio-notes";
    static final String METADATA_KEY = "local_audio";
    private static final long MAX_AUDIO_BYTES = 32L * 1024L * 1024L;

    private final File root;

    ReaderAudioNoteRepository(Context context) {
        root = new File(context.getApplicationContext().getFilesDir(), ROOT_NAME);
    }

    File createRecordingTarget() throws Exception {
        File directory = newCaptureDirectory();
        return new File(directory, "original.m4a");
    }

    SavedAudio save(byte[] bytes, String mimeType) throws Exception {
        if (bytes == null || bytes.length == 0 || bytes.length > MAX_AUDIO_BYTES) {
            throw new IllegalArgumentException("invalid reader audio size");
        }
        String extension = "audio/wav".equals(clean(mimeType)) ? "wav" : "m4a";
        File directory = newCaptureDirectory();
        File target = new File(directory, "original." + extension);
        File pending = new File(directory, target.getName() + ".part");
        try (FileOutputStream output = new FileOutputStream(pending, false)) {
            output.write(bytes);
            output.flush();
            output.getFD().sync();
        }
        if (!pending.renameTo(target)) {
            pending.delete();
            throw new IllegalStateException("cannot commit reader audio");
        }
        return commit(target, mimeType);
    }

    SavedAudio commit(File candidate, String mimeType) throws Exception {
        File controlled = controlledFile(candidate);
        if (!controlled.isFile()
                || controlled.length() <= 0L
                || controlled.length() > MAX_AUDIO_BYTES) {
            throw new IllegalArgumentException("reader audio is missing or invalid");
        }
        String relative = relativePath(controlled);
        return new SavedAudio(
                relative,
                VerifiedAssetFile.sha256(controlled),
                controlled.length(),
                clean(mimeType)
        );
    }

    JSONObject metadata(JSONObject base, SavedAudio audio) throws Exception {
        JSONObject result = base == null ? new JSONObject() : new JSONObject(base.toString());
        result.put(
                METADATA_KEY,
                new JSONObject()
                        .put("relative_path", audio.relativePath)
                        .put("sha256", audio.sha256)
                        .put("byte_size", audio.byteSize)
                        .put("mime_type", audio.mimeType)
        );
        return result;
    }

    File resolve(JSONObject annotationMetadata) throws Exception {
        JSONObject local = annotationMetadata == null
                ? null
                : annotationMetadata.optJSONObject(METADATA_KEY);
        if (local == null) {
            return null;
        }
        String relative = clean(local.optString("relative_path"));
        String hash = clean(local.optString("sha256")).toLowerCase(Locale.ROOT);
        long byteSize = local.optLong("byte_size", -1L);
        if (!relative.matches("[0-9a-f-]{36}/original\\.(m4a|wav)")
                || !hash.matches("[0-9a-f]{64}")
                || byteSize <= 0L
                || byteSize > MAX_AUDIO_BYTES) {
            throw new IllegalStateException("invalid local reader audio metadata");
        }
        File file = controlledFile(new File(root, relative));
        VerifiedAssetFile.verify(file, hash, byteSize);
        return file;
    }

    void delete(File candidate) {
        try {
            File controlled = controlledFile(candidate);
            File directory = controlled.getParentFile();
            if (controlled.exists()) {
                controlled.delete();
            }
            if (directory != null && directory.isDirectory()) {
                directory.delete();
            }
        } catch (Throwable ignored) {
            // A future explicit cleanup can remove an orphan; never widen deletion scope.
        }
    }

    void delete(SavedAudio audio) {
        if (audio == null) {
            return;
        }
        try {
            delete(new File(root, audio.relativePath));
        } catch (Throwable ignored) {
            // Bounded orphan cleanup is intentionally deferred to an explicit future maintenance run.
        }
    }

    private File newCaptureDirectory() throws Exception {
        if (!root.isDirectory() && !root.mkdirs()) {
            throw new IllegalStateException("cannot create reader audio root");
        }
        File directory = new File(root, UUID.randomUUID().toString());
        if (!directory.mkdir()) {
            throw new IllegalStateException("cannot create reader audio directory");
        }
        return directory.getCanonicalFile();
    }

    private File controlledFile(File candidate) throws Exception {
        if (candidate == null) {
            throw new IllegalArgumentException("reader audio path is required");
        }
        File canonicalRoot = root.getCanonicalFile();
        File canonicalFile = candidate.getCanonicalFile();
        if (!canonicalFile.getPath().startsWith(canonicalRoot.getPath() + File.separator)
                || canonicalFile.getParentFile() == null
                || !canonicalFile.getParentFile().getParentFile().equals(canonicalRoot)) {
            throw new SecurityException("reader audio is outside the controlled root");
        }
        return canonicalFile;
    }

    private String relativePath(File file) throws Exception {
        String prefix = root.getCanonicalPath() + File.separator;
        String path = file.getCanonicalPath();
        if (!path.startsWith(prefix)) {
            throw new SecurityException("reader audio is outside the controlled root");
        }
        return path.substring(prefix.length());
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }

    static final class SavedAudio {
        final String relativePath;
        final String sha256;
        final long byteSize;
        final String mimeType;

        SavedAudio(String relativePath, String sha256, long byteSize, String mimeType) {
            this.relativePath = relativePath;
            this.sha256 = sha256;
            this.byteSize = byteSize;
            this.mimeType = mimeType;
        }
    }
}
