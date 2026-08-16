package com.click.shell;

import android.content.Context;
import android.util.AtomicFile;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.regex.Pattern;

/**
 * File-backed source of truth for Android Tingle captures.
 *
 * <p>The repository deliberately keeps the existing
 * {@code files/voice-inbox-pending/<capture-id>/original.wav + capture.json}
 * layout. It owns no database and never places audio in the reader operation queue.</p>
 */
final class TingleCaptureRepository {
    static final String ROOT_DIRECTORY_NAME = "voice-inbox-pending";
    static final String AUDIO_FILE_NAME = "original.wav";
    static final String MANIFEST_FILE_NAME = "capture.json";
    static final String MANIFEST_SCHEMA = "click.tingle.capture.v2";

    static final String SYNC_PENDING = "pending";
    static final String SYNC_METADATA_PENDING = "metadata_pending";
    static final String SYNC_UPLOADING = "uploading";
    static final String SYNC_FAILED = "failed";
    static final String SYNC_SYNCED = "synced";

    private static final String UPLOAD_MODE_TINGLE = "recording";
    private static final int MAX_CAPTURE_DIRECTORIES = 5000;
    private static final int MAX_MANIFEST_BYTES = 256 * 1024;
    private static final int MAX_TITLE_CHARACTERS = 160;
    private static final int MAX_NOTE_CHARACTERS = 4000;
    private static final int MAX_ERROR_CHARACTERS = 800;
    private static final Pattern CAPTURE_ID_PATTERN =
            Pattern.compile("^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$");
    private static final Object PROCESS_CAPTURE_LOCK = new Object();

    private final File rootDirectory;
    private final Context appContext;

    TingleCaptureRepository(Context context) {
        Context application = context.getApplicationContext();
        this.rootDirectory = new File(application.getFilesDir(), ROOT_DIRECTORY_NAME);
        this.appContext = application;
    }

    /** Direct-root constructor for isolated unit tests. */
    TingleCaptureRepository(File rootDirectory) {
        if (rootDirectory == null) {
            throw new IllegalArgumentException("Tingle capture root is required");
        }
        this.rootDirectory = rootDirectory;
        this.appContext = null;
    }

    synchronized Capture beginCapture(String deviceId) throws IOException {
        synchronized (PROCESS_CAPTURE_LOCK) {
            ensureRootDirectory();
            String captureId = newCaptureId();
            File directory = safeCaptureDirectory(captureId);
            if (!directory.mkdir()) {
                throw new IOException("Cannot create Tingle capture directory");
            }
            String now = nowIso();
            JSONObject manifest = new JSONObject();
            putJson(manifest, "schema", MANIFEST_SCHEMA);
            putJson(manifest, "capture_id", captureId);
            putJson(manifest, "device_id", clean(deviceId));
            putJson(manifest, "upload_mode", UPLOAD_MODE_TINGLE);
            putJson(manifest, "mime_type", "audio/wav");
            putJson(manifest, "state", "recording");
            putJson(manifest, "sync_state", SYNC_PENDING);
            putJson(manifest, "title", "");
            putJson(manifest, "note", "");
            putJson(manifest, "server_recording_id", "");
            putJson(manifest, "audio_hash", "");
            putJson(manifest, "duration_seconds", 0.0);
            putJson(manifest, "attempt_count", 0);
            putJson(manifest, "metadata_revision", 0);
            putJson(manifest, "retryable", true);
            putJson(manifest, "last_error", "");
            putJson(manifest, "created_at", now);
            putJson(manifest, "updated_at", now);
            writeManifest(directory, manifest);
            return captureFromManifest(directory, manifest);
        }
    }

    synchronized Capture markReady(String captureId, double durationSeconds) throws IOException {
        synchronized (PROCESS_CAPTURE_LOCK) {
            File directory = safeCaptureDirectory(captureId);
            File audioFile = new File(directory, AUDIO_FILE_NAME);
            if (!audioFile.isFile() || audioFile.length() <= 44L) {
                throw new IOException("Tingle recording has no audio samples");
            }
            JSONObject manifest = requireTingleManifest(directory);
            putJson(manifest, "state", "pending_upload");
            putJson(manifest, "sync_state", SYNC_PENDING);
            putJson(manifest, "duration_seconds", Math.max(0.0, durationSeconds));
            putJson(manifest, "audio_hash", sha256(audioFile));
            putJson(manifest, "retryable", true);
            putJson(manifest, "last_error", "");
            return persist(directory, manifest);
        }
    }

    synchronized Capture markRecordingFailure(String captureId, Throwable error) throws IOException {
        synchronized (PROCESS_CAPTURE_LOCK) {
            File directory = safeCaptureDirectory(captureId);
            JSONObject manifest = requireTingleManifest(directory);
            putJson(manifest, "state", "recording_failed");
            putJson(manifest, "sync_state", SYNC_FAILED);
            putJson(manifest, "retryable", false);
            putJson(manifest, "last_error", bounded(errorMessage(error), MAX_ERROR_CHARACTERS));
            return persist(directory, manifest);
        }
    }

    synchronized Capture updateMetadata(String captureId, String title, String note) throws IOException {
        Capture updated;
        synchronized (PROCESS_CAPTURE_LOCK) {
            File directory = safeCaptureDirectory(captureId);
            JSONObject manifest = requireTingleManifest(directory);
            putJson(manifest, "title", bounded(clean(title), MAX_TITLE_CHARACTERS));
            putJson(manifest, "note", bounded(clean(note), MAX_NOTE_CHARACTERS));
            putJson(
                    manifest,
                    "metadata_revision",
                    Math.max(0, manifest.optInt("metadata_revision", 0)) + 1
            );
            if (!clean(manifest.optString("server_recording_id", "")).isEmpty()) {
                putJson(manifest, "state", "metadata_pending");
                putJson(manifest, "sync_state", SYNC_METADATA_PENDING);
                putJson(manifest, "retryable", true);
                putJson(manifest, "last_error", "");
            }
            updated = persist(directory, manifest);
        }
        if (appContext != null && SYNC_METADATA_PENDING.equals(updated.syncState)) {
            TingleUploadScheduler.enqueue(appContext);
        }
        return updated;
    }

    synchronized Capture ensureAudioHash(String captureId) throws IOException {
        synchronized (PROCESS_CAPTURE_LOCK) {
            File directory = safeCaptureDirectory(captureId);
            JSONObject manifest = requireTingleManifest(directory);
            String currentHash = clean(manifest.optString("audio_hash", ""));
            if (!currentHash.isEmpty()) {
                return captureFromManifest(directory, manifest);
            }
            File audioFile = new File(directory, AUDIO_FILE_NAME);
            if (!audioFile.isFile() || audioFile.length() <= 44L) {
                throw new IOException("Tingle recording is unavailable");
            }
            putJson(manifest, "audio_hash", sha256(audioFile));
            return persist(directory, manifest);
        }
    }

    synchronized Capture markUploading(String captureId) throws IOException {
        synchronized (PROCESS_CAPTURE_LOCK) {
            File directory = safeCaptureDirectory(captureId);
            JSONObject manifest = requireTingleManifest(directory);
            boolean metadataOnly = manifest.optBoolean("metadata_only_upload", false)
                    || clean(manifest.optString("state", "")).startsWith("metadata_")
                    || SYNC_METADATA_PENDING.equals(
                            clean(manifest.optString("sync_state", ""))
                    );
            putJson(manifest, "state", metadataOnly ? "metadata_uploading" : "uploading");
            putJson(manifest, "sync_state", SYNC_UPLOADING);
            putJson(manifest, "metadata_only_upload", metadataOnly);
            putJson(manifest, "attempt_count", Math.max(0, manifest.optInt("attempt_count", 0)) + 1);
            putJson(manifest, "last_error", "");
            return persist(directory, manifest);
        }
    }

    synchronized Capture markUploadFailure(
            String captureId,
            String error,
            boolean retryable
    ) throws IOException {
        synchronized (PROCESS_CAPTURE_LOCK) {
            File directory = safeCaptureDirectory(captureId);
            JSONObject manifest = requireTingleManifest(directory);
            putJson(
                    manifest,
                    "state",
                    manifest.optBoolean("metadata_only_upload", false)
                            ? "metadata_upload_failed"
                            : "upload_failed"
            );
            putJson(manifest, "sync_state", SYNC_FAILED);
            putJson(manifest, "retryable", retryable);
            putJson(manifest, "last_error", bounded(clean(error), MAX_ERROR_CHARACTERS));
            return persist(directory, manifest);
        }
    }

    /**
     * Persists a verified server receipt without deleting {@code original.wav}.
     *
     * <p>If the process dies after the server commits but before this write, the next attempt uses
     * the same client capture ID and hash; the server returns the existing logical record.</p>
     */
    synchronized Capture markSynced(
            String captureId,
            String serverRecordingId,
            String audioHash,
            boolean duplicate,
            int uploadedMetadataRevision
    ) throws IOException {
        synchronized (PROCESS_CAPTURE_LOCK) {
            File directory = safeCaptureDirectory(captureId);
            JSONObject manifest = requireTingleManifest(directory);
            String localHash = clean(manifest.optString("audio_hash", ""));
            String verifiedHash = clean(audioHash).toLowerCase(Locale.ROOT);
            if (localHash.isEmpty() || !localHash.equalsIgnoreCase(verifiedHash)) {
                throw new IOException("Server receipt audio hash does not match the local original");
            }
            if (clean(serverRecordingId).isEmpty()) {
                throw new IOException("Server receipt is missing recording_id");
            }
            putJson(manifest, "state", "synced");
            putJson(manifest, "sync_state", SYNC_SYNCED);
            putJson(manifest, "server_recording_id", clean(serverRecordingId));
            putJson(manifest, "audio_hash", verifiedHash);
            putJson(manifest, "server_duplicate", duplicate);
            putJson(manifest, "synced_at", nowIso());
            boolean newerMetadataPending =
                    Math.max(0, manifest.optInt("metadata_revision", 0))
                            > Math.max(0, uploadedMetadataRevision);
            putJson(manifest, "metadata_only_upload", false);
            putJson(manifest, "retryable", newerMetadataPending);
            putJson(manifest, "last_error", "");
            if (newerMetadataPending) {
                putJson(manifest, "state", "metadata_pending");
                putJson(manifest, "sync_state", SYNC_METADATA_PENDING);
            } else {
                putJson(manifest, "state", "synced");
                putJson(manifest, "sync_state", SYNC_SYNCED);
            }
            return persist(directory, manifest);
        }
    }

    synchronized Capture findCapture(String captureId) throws IOException {
        synchronized (PROCESS_CAPTURE_LOCK) {
            File directory = safeCaptureDirectory(captureId);
            if (!directory.isDirectory()) {
                return null;
            }
            JSONObject manifest = readManifest(directory);
            if (manifest == null || !isTingleManifest(manifest)) {
                return null;
            }
            return captureFromManifest(directory, manifest);
        }
    }

    synchronized List<Capture> listCaptures() {
        synchronized (PROCESS_CAPTURE_LOCK) {
            try {
                ensureRootDirectory();
                File[] directories = rootDirectory.listFiles(File::isDirectory);
                if (directories == null || directories.length == 0) {
                    return Collections.emptyList();
                }
                List<Capture> captures = new ArrayList<>();
                int inspected = 0;
                for (File directory : directories) {
                    if (inspected++ >= MAX_CAPTURE_DIRECTORIES || !isSafeDirectChild(directory)) {
                        continue;
                    }
                    JSONObject manifest = readManifest(directory);
                    if (manifest == null || !isTingleManifest(manifest)) {
                        continue;
                    }
                    captures.add(captureFromManifest(directory, manifest));
                }
                captures.sort(
                        Comparator.comparingLong((Capture capture) -> capture.updatedAtMillis)
                                .thenComparing(capture -> capture.captureId)
                );
                return Collections.unmodifiableList(captures);
            } catch (Throwable ignored) {
                return Collections.emptyList();
            }
        }
    }

    synchronized List<Capture> pendingCaptures() {
        List<Capture> pending = new ArrayList<>();
        for (Capture capture : listCaptures()) {
            if (capture.isEligibleForUpload()) {
                pending.add(capture);
            }
        }
        return Collections.unmodifiableList(pending);
    }

    synchronized int pendingCount() {
        return pendingCaptures().size();
    }

    /**
     * Converts process-interrupted Tingle WAVs into durable pending captures.
     *
     * <p>Only manifests explicitly belonging to Tingle are touched. Reader-note and Hermes voice
     * captures share the root but keep their existing owner and upload contract.</p>
     */
    synchronized int recoverInterruptedCaptures() throws IOException {
        synchronized (PROCESS_CAPTURE_LOCK) {
            ensureRootDirectory();
            File[] directories = rootDirectory.listFiles(File::isDirectory);
            if (directories == null || directories.length == 0) {
                return 0;
            }
            int recovered = 0;
            int inspected = 0;
            for (File directory : directories) {
                if (inspected++ >= MAX_CAPTURE_DIRECTORIES || !isSafeDirectChild(directory)) {
                    continue;
                }
                JSONObject manifest = readManifest(directory);
                if (manifest == null) {
                    manifest = recoverLegacyTemporaryManifest(directory);
                }
                if (manifest == null || !isTingleManifest(manifest)
                        || !"recording".equals(clean(manifest.optString("state", "")))) {
                    continue;
                }
                String captureId = directory.getName();
                File audioFile = new File(directory, AUDIO_FILE_NAME);
                if (!audioFile.isFile() || audioFile.length() <= 44L) {
                    markRecordingFailure(captureId, new IOException("Interrupted recording has no audio samples"));
                    continue;
                }
                repairWavHeader(audioFile);
                double durationSeconds = Math.max(0.1, (audioFile.length() - 44L) / 32000.0);
                markReady(captureId, durationSeconds);
                recovered += 1;
            }
            return recovered;
        }
    }

    static boolean isSafeCaptureId(String value) {
        return value != null && CAPTURE_ID_PATTERN.matcher(value).matches();
    }

    static String verificationCode(String captureId) {
        if (!isSafeCaptureId(captureId)) {
            throw new IllegalArgumentException("Invalid Tingle capture ID");
        }
        try {
            byte[] hash = MessageDigest.getInstance("SHA-256")
                    .digest(captureId.getBytes(StandardCharsets.UTF_8));
            StringBuilder code = new StringBuilder(12);
            for (int index = 0; index < 6; index += 1) {
                code.append(String.format(Locale.ROOT, "%02X", hash[index] & 0xff));
            }
            return code.toString();
        } catch (java.security.NoSuchAlgorithmException error) {
            throw new IllegalStateException("SHA-256 is unavailable", error);
        }
    }

    private Capture persist(File directory, JSONObject manifest) throws IOException {
        normalizeForV2(directory, manifest);
        putJson(manifest, "updated_at", nowIso());
        writeManifest(directory, manifest);
        return captureFromManifest(directory, manifest);
    }

    private JSONObject requireTingleManifest(File directory) throws IOException {
        if (!directory.isDirectory()) {
            throw new IOException("Tingle capture does not exist");
        }
        JSONObject manifest = readManifest(directory);
        if (manifest == null) {
            throw new IOException("Tingle capture manifest is missing or invalid");
        }
        if (!isTingleManifest(manifest)) {
            throw new IOException("Capture belongs to another voice feature");
        }
        return manifest;
    }

    private boolean isTingleManifest(JSONObject manifest) {
        String uploadMode = clean(manifest.optString("upload_mode", UPLOAD_MODE_TINGLE));
        return uploadMode.isEmpty() || UPLOAD_MODE_TINGLE.equals(uploadMode);
    }

    private Capture captureFromManifest(File directory, JSONObject manifest) {
        String state = clean(manifest.optString("state", "pending_upload"));
        String syncState = clean(manifest.optString("sync_state", ""));
        if (syncState.isEmpty()) {
            if ("synced".equals(state)) {
                syncState = SYNC_SYNCED;
            } else if ("upload_failed".equals(state) || "recording_failed".equals(state)) {
                syncState = SYNC_FAILED;
            } else {
                syncState = SYNC_PENDING;
            }
        }
        String captureId = directory.getName();
        File audioFile = new File(directory, AUDIO_FILE_NAME);
        String title = bounded(clean(manifest.optString("title", "")), MAX_TITLE_CHARACTERS);
        String note = bounded(clean(manifest.optString("note", "")), MAX_NOTE_CHARACTERS);
        return new Capture(
                captureId,
                audioFile,
                clean(manifest.optString("device_id", "")),
                title,
                note,
                state,
                syncState,
                clean(manifest.optString("server_recording_id", "")),
                clean(manifest.optString("audio_hash", "")),
                Math.max(0.0, manifest.optDouble("duration_seconds", 0.0)),
                Math.max(0, manifest.optInt("attempt_count", 0)),
                Math.max(0, manifest.optInt("metadata_revision", 0)),
                manifest.has("retryable") ? manifest.optBoolean("retryable", true) : true,
                bounded(clean(manifest.optString("last_error", "")), MAX_ERROR_CHARACTERS),
                clean(manifest.optString("created_at", "")),
                clean(manifest.optString("updated_at", "")),
                new File(directory, MANIFEST_FILE_NAME).lastModified()
        );
    }

    private void normalizeForV2(File directory, JSONObject manifest) throws IOException {
        putJson(manifest, "schema", MANIFEST_SCHEMA);
        putJson(manifest, "capture_id", directory.getName());
        putJson(manifest, "upload_mode", UPLOAD_MODE_TINGLE);
        putJson(manifest, "mime_type", "audio/wav");
        putDefault(manifest, "device_id", "");
        putDefault(manifest, "state", "pending_upload");
        putDefault(manifest, "sync_state", SYNC_PENDING);
        putDefault(manifest, "title", "");
        putDefault(manifest, "note", "");
        putDefault(manifest, "server_recording_id", "");
        putDefault(manifest, "audio_hash", "");
        putDefault(manifest, "duration_seconds", 0.0);
        putDefault(manifest, "attempt_count", 0);
        putDefault(manifest, "metadata_revision", 0);
        putDefault(manifest, "retryable", true);
        putDefault(manifest, "last_error", "");
        putDefault(manifest, "created_at", nowIso());
    }

    private void putDefault(JSONObject manifest, String key, Object value) throws IOException {
        if (!manifest.has(key) || manifest.isNull(key)) {
            putJson(manifest, key, value);
        }
    }

    private JSONObject recoverLegacyTemporaryManifest(File directory) throws IOException {
        File legacyTemporary = new File(directory, MANIFEST_FILE_NAME + ".tmp");
        JSONObject manifest = readJsonFile(legacyTemporary);
        if (manifest == null || !isTingleManifest(manifest)) {
            return null;
        }
        writeManifest(directory, manifest);
        return manifest;
    }

    private JSONObject readManifest(File directory) {
        File manifestFile = new File(directory, MANIFEST_FILE_NAME);
        return readJsonFile(manifestFile);
    }

    private JSONObject readJsonFile(File manifestFile) {
        if (!manifestFile.isFile() || manifestFile.length() <= 0L
                || manifestFile.length() > MAX_MANIFEST_BYTES) {
            return null;
        }
        AtomicFile atomicFile = new AtomicFile(manifestFile);
        try (InputStream input = atomicFile.openRead()) {
            ByteArrayOutputStream output = new ByteArrayOutputStream(
                    (int) Math.min(manifestFile.length(), 16 * 1024L)
            );
            byte[] buffer = new byte[8192];
            int total = 0;
            int read;
            while ((read = input.read(buffer)) >= 0) {
                total += read;
                if (total > MAX_MANIFEST_BYTES) {
                    return null;
                }
                output.write(buffer, 0, read);
            }
            return new JSONObject(output.toString(StandardCharsets.UTF_8.name()));
        } catch (Throwable ignored) {
            return null;
        }
    }

    static void repairWavHeader(File audioFile) throws IOException {
        ClickWavRecorder.repairHeader(audioFile);
    }

    private static void putJson(JSONObject object, String key, Object value) throws IOException {
        try {
            object.put(key, value);
        } catch (JSONException error) {
            throw new IOException("Cannot encode Tingle capture manifest", error);
        }
    }

    private void writeManifest(File directory, JSONObject manifest) throws IOException {
        if (!isSafeDirectChild(directory)) {
            throw new IOException("Unsafe Tingle capture directory");
        }
        byte[] payload = manifest.toString().getBytes(StandardCharsets.UTF_8);
        if (payload.length > MAX_MANIFEST_BYTES) {
            throw new IOException("Tingle capture manifest is too large");
        }
        AtomicFile atomicFile = new AtomicFile(new File(directory, MANIFEST_FILE_NAME));
        FileOutputStream output = null;
        try {
            output = atomicFile.startWrite();
            output.write(payload);
            output.getFD().sync();
            atomicFile.finishWrite(output);
        } catch (Throwable error) {
            if (output != null) {
                atomicFile.failWrite(output);
            }
            if (error instanceof IOException) {
                throw (IOException) error;
            }
            throw new IOException("Cannot persist Tingle capture manifest", error);
        }
    }

    private void ensureRootDirectory() throws IOException {
        if (!rootDirectory.isDirectory() && !rootDirectory.mkdirs()) {
            throw new IOException("Cannot create Tingle capture root");
        }
        if (!isSafeRootDirectory(rootDirectory)) {
            throw new IOException("Tingle capture root must not be a symbolic link");
        }
    }

    static boolean isSafeRootDirectory(File root) throws IOException {
        if (root == null) {
            return false;
        }
        File absoluteParent = root.getAbsoluteFile().getParentFile();
        if (absoluteParent == null) {
            return false;
        }
        File expectedRoot = new File(absoluteParent.getCanonicalFile(), root.getName());
        return root.getCanonicalFile().equals(expectedRoot);
    }

    private File safeCaptureDirectory(String captureId) throws IOException {
        if (!isSafeCaptureId(captureId)) {
            throw new IOException("Invalid Tingle capture ID");
        }
        ensureRootDirectory();
        File candidate = new File(rootDirectory, captureId);
        if (!candidate.getCanonicalFile().getParentFile().equals(rootDirectory.getCanonicalFile())) {
            throw new IOException("Tingle capture path escapes its root");
        }
        return candidate;
    }

    private boolean isSafeDirectChild(File directory) {
        try {
            return isSafeDirectChild(rootDirectory, directory);
        } catch (IOException ignored) {
            return false;
        }
    }

    static boolean isSafeDirectChild(File root, File directory) throws IOException {
        if (root == null || directory == null || !isSafeCaptureId(directory.getName())) {
            return false;
        }
        File absoluteParent = directory.getAbsoluteFile().getParentFile();
        if (absoluteParent == null) {
            return false;
        }
        File canonicalParent = absoluteParent.getCanonicalFile();
        File expectedDirectory = new File(canonicalParent, directory.getName());
        return canonicalParent.equals(root.getCanonicalFile())
                && directory.getCanonicalFile().equals(expectedDirectory);
    }

    private String newCaptureId() {
        return "android-"
                + Long.toString(System.currentTimeMillis(), 36)
                + "-"
                + UUID.randomUUID().toString().replace("-", "");
    }

    private String sha256(File file) throws IOException {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            try (InputStream input = new FileInputStream(file)) {
                byte[] buffer = new byte[64 * 1024];
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    digest.update(buffer, 0, read);
                }
            }
            StringBuilder hex = new StringBuilder(64);
            for (byte value : digest.digest()) {
                hex.append(String.format(Locale.ROOT, "%02x", value & 0xff));
            }
            return hex.toString();
        } catch (IOException error) {
            throw error;
        } catch (Throwable error) {
            throw new IOException("Cannot hash Tingle original audio", error);
        }
    }

    private static String nowIso() {
        return Instant.now().toString();
    }

    private static String errorMessage(Throwable error) {
        if (error == null) {
            return "Recording failed";
        }
        String message = clean(error.getMessage());
        return message.isEmpty() ? error.getClass().getSimpleName() : message;
    }

    private static String bounded(String value, int maxCharacters) {
        String clean = clean(value);
        return clean.length() <= maxCharacters ? clean : clean.substring(0, maxCharacters);
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }

    static final class Capture {
        final String captureId;
        final File audioFile;
        final String deviceId;
        final String title;
        final String note;
        final String state;
        final String syncState;
        final String serverRecordingId;
        final String audioHash;
        final double durationSeconds;
        final int attemptCount;
        final int metadataRevision;
        final boolean retryable;
        final String lastError;
        final String createdAt;
        final String updatedAt;
        final long updatedAtMillis;

        Capture(
                String captureId,
                File audioFile,
                String deviceId,
                String title,
                String note,
                String state,
                String syncState,
                String serverRecordingId,
                String audioHash,
                double durationSeconds,
                int attemptCount,
                int metadataRevision,
                boolean retryable,
                String lastError,
                String createdAt,
                String updatedAt,
                long updatedAtMillis
        ) {
            this.captureId = captureId;
            this.audioFile = audioFile;
            this.deviceId = deviceId;
            this.title = title;
            this.note = note;
            this.state = state;
            this.syncState = syncState;
            this.serverRecordingId = serverRecordingId;
            this.audioHash = audioHash;
            this.durationSeconds = durationSeconds;
            this.attemptCount = attemptCount;
            this.metadataRevision = metadataRevision;
            this.retryable = retryable;
            this.lastError = lastError;
            this.createdAt = createdAt;
            this.updatedAt = updatedAt;
            this.updatedAtMillis = updatedAtMillis;
        }

        boolean hasPlayableAudio() {
            return audioFile.isFile() && audioFile.length() > 44L;
        }

        boolean isEligibleForUpload() {
            if (SYNC_SYNCED.equals(syncState) || "recording".equals(state)
                    || "recording_failed".equals(state) || !hasPlayableAudio()) {
                return false;
            }
            return !SYNC_FAILED.equals(syncState) || retryable;
        }

        boolean isMetadataOnlyUpload() {
            return state.startsWith("metadata_") || SYNC_METADATA_PENDING.equals(syncState);
        }
    }
}
