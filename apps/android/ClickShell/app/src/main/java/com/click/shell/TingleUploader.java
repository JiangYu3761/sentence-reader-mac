package com.click.shell;

import android.util.Base64;
import android.util.Base64OutputStream;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * Serial, receipt-driven uploader for file-backed Tingle captures.
 *
 * <p>Network delivery can be repeated after an ambiguous failure. Logical insertion remains
 * exactly once because every request reuses the same {@code device_id + client_capture_id} and
 * the server rejects a hash mismatch. A capture is marked synced only after its recording ID and
 * audio hash are verified.</p>
 */
final class TingleUploader {
    private static final int CONNECT_TIMEOUT_MILLIS = 15_000;
    private static final int READ_TIMEOUT_MILLIS = 180_000;
    private static final int RESPONSE_LIMIT_BYTES = 2 * 1024 * 1024;
    static final int MAX_CAPTURES_PER_RUN = 1;

    private final TingleCaptureRepository repository;
    private final ClickSyncConfig config;
    private final Transport transport;

    TingleUploader(TingleCaptureRepository repository, ClickSyncConfig config) {
        this(repository, config, new HttpTransport());
    }

    TingleUploader(
            TingleCaptureRepository repository,
            ClickSyncConfig config,
            Transport transport
    ) {
        this.repository = repository;
        this.config = config;
        this.transport = transport;
    }

    Outcome uploadPending() {
        if (config == null || !config.isReady()) {
            return Outcome.of(
                    Outcome.Status.AUTH_REQUIRED,
                    0,
                    0,
                    repository.pendingCount(),
                    "Click 尚未完成设备授权"
            );
        }
        TingleUploadRunGate.Lease lease = TingleUploadRunGate.tryAcquire();
        if (lease == null) {
            return Outcome.of(
                    Outcome.Status.BUSY,
                    0,
                    0,
                    repository.pendingCount(),
                    "Tingle 上传已在当前进程运行"
            );
        }
        try (TingleUploadRunGate.Lease ignored = lease) {
            return uploadSerially();
        }
    }

    private Outcome uploadSerially() {
        List<TingleCaptureRepository.Capture> pending = repository.pendingCaptures();
        if (pending.isEmpty()) {
            return Outcome.of(Outcome.Status.SUCCESS, 0, 0, 0, "没有待同步录音");
        }

        int uploaded = 0;
        int permanentlyFailed = 0;
        String lastError = "";
        int runCount = Math.min(MAX_CAPTURES_PER_RUN, pending.size());
        for (int index = 0; index < runCount; index++) {
            TingleCaptureRepository.Capture queuedCapture = pending.get(index);
            TingleCaptureRepository.Capture capture;
            try {
                capture = repository.ensureAudioHash(queuedCapture.captureId);
                capture = repository.markUploading(capture.captureId);
            } catch (IOException error) {
                lastError = compact(error);
                try {
                    repository.markUploadFailure(queuedCapture.captureId, lastError, true);
                } catch (IOException manifestError) {
                    lastError = compact(manifestError);
                }
                return Outcome.of(
                        Outcome.Status.RETRYABLE_FAILURE,
                        uploaded,
                        permanentlyFailed,
                        repository.pendingCount(),
                        lastError
                );
            }

            try {
                ServerReceipt receipt = capture.isMetadataOnlyUpload()
                        ? transport.updateMetadata(capture, config)
                        : transport.upload(capture, config);
                repository.markSynced(
                        capture.captureId,
                        receipt.recordingId,
                        receipt.audioHash,
                        receipt.duplicate,
                        capture.metadataRevision
                );
                uploaded += 1;
            } catch (UploadFailure error) {
                lastError = compact(error);
                boolean retryable = canRetryAfterExternalEvent(error.kind);
                try {
                    repository.markUploadFailure(capture.captureId, lastError, retryable);
                } catch (IOException manifestError) {
                    lastError = compact(manifestError);
                    return Outcome.of(
                            Outcome.Status.RETRYABLE_FAILURE,
                            uploaded,
                            permanentlyFailed,
                            repository.pendingCount(),
                            lastError
                    );
                }
                if (error.kind == FailureKind.AUTH_REQUIRED) {
                    return Outcome.of(
                            Outcome.Status.AUTH_REQUIRED,
                            uploaded,
                            permanentlyFailed,
                            repository.pendingCount(),
                            lastError
                    );
                }
                if (retryable) {
                    return Outcome.of(
                            Outcome.Status.RETRYABLE_FAILURE,
                            uploaded,
                            permanentlyFailed,
                            repository.pendingCount(),
                            lastError
                    );
                }
                permanentlyFailed += 1;
            } catch (IOException error) {
                lastError = compact(error);
                try {
                    repository.markUploadFailure(capture.captureId, lastError, true);
                } catch (IOException ignored) {
                    // WorkManager retries; the original audio is never deleted here.
                }
                return Outcome.of(
                        Outcome.Status.RETRYABLE_FAILURE,
                        uploaded,
                        permanentlyFailed,
                        repository.pendingCount(),
                        lastError
                );
            }
        }

        if (permanentlyFailed > 0) {
            return Outcome.of(
                    Outcome.Status.PERMANENT_FAILURE,
                    uploaded,
                    permanentlyFailed,
                    repository.pendingCount(),
                    lastError
            );
        }
        return Outcome.of(
                Outcome.Status.SUCCESS,
                uploaded,
                0,
                repository.pendingCount(),
                uploaded == 1 ? "已同步 1 条录音" : "已同步 " + uploaded + " 条录音"
        );
    }

    static FailureKind classifyHttpStatus(int statusCode) {
        if (statusCode == 401 || statusCode == 403) {
            return FailureKind.AUTH_REQUIRED;
        }
        if (statusCode == 408 || statusCode == 425 || statusCode == 429
                || statusCode >= 500) {
            return FailureKind.RETRYABLE;
        }
        return FailureKind.PERMANENT;
    }

    static boolean canRetryAfterExternalEvent(FailureKind kind) {
        return kind != FailureKind.PERMANENT;
    }

    interface Transport {
        ServerReceipt upload(
                TingleCaptureRepository.Capture capture,
                ClickSyncConfig config
        ) throws IOException, UploadFailure;

        ServerReceipt updateMetadata(
                TingleCaptureRepository.Capture capture,
                ClickSyncConfig config
        ) throws IOException, UploadFailure;
    }

    static final class HttpTransport implements Transport {
        @Override
        public ServerReceipt upload(
                TingleCaptureRepository.Capture capture,
                ClickSyncConfig config
        ) throws IOException, UploadFailure {
            String endpoint = ReaderApiUrlPolicy.resolve(config.baseUrl, "/v1/recordings");
            HttpURLConnection connection = (HttpURLConnection) new URL(endpoint).openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setConnectTimeout(CONNECT_TIMEOUT_MILLIS);
            connection.setReadTimeout(READ_TIMEOUT_MILLIS);
            connection.setRequestMethod("POST");
            connection.setDoOutput(true);
            connection.setChunkedStreamingMode(64 * 1024);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("X-Click-Device-Id", config.deviceId);
            connection.setRequestProperty("X-Click-Access-Token", config.accessToken);
            connection.setRequestProperty("Authorization", "Bearer " + config.accessToken);

            try {
                writeRequestBody(connection, capture, config.deviceId);
                int statusCode = connection.getResponseCode();
                String responseText = readResponse(connection, statusCode);
                if (statusCode < 200 || statusCode >= 300) {
                    throw new UploadFailure(
                            classifyHttpStatus(statusCode),
                            "Click Runtime 返回 " + statusCode + ": " + compactResponse(responseText)
                    );
                }
                return parseReceipt(responseText, capture.audioHash);
            } catch (SocketTimeoutException error) {
                throw new UploadFailure(FailureKind.RETRYABLE, "连接 Click Runtime 超时", error);
            } finally {
                connection.disconnect();
            }
        }

        @Override
        public ServerReceipt updateMetadata(
                TingleCaptureRepository.Capture capture,
                ClickSyncConfig config
        ) throws IOException, UploadFailure {
            if (capture.serverRecordingId.isEmpty()) {
                throw new UploadFailure(
                        FailureKind.PERMANENT,
                        "Tingle metadata update is missing recording_id"
                );
            }
            String endpoint = ReaderApiUrlPolicy.resolve(
                    config.baseUrl,
                    "/v1/recordings/" + urlSegment(capture.serverRecordingId)
            );
            HttpURLConnection connection = (HttpURLConnection) new URL(endpoint).openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setConnectTimeout(CONNECT_TIMEOUT_MILLIS);
            connection.setReadTimeout(READ_TIMEOUT_MILLIS);
            connection.setRequestMethod("PATCH");
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("X-Click-Device-Id", config.deviceId);
            connection.setRequestProperty("X-Click-Access-Token", config.accessToken);
            connection.setRequestProperty("Authorization", "Bearer " + config.accessToken);
            try {
                JSONObject payload = new JSONObject();
                try {
                    payload.put("title", capture.title);
                    payload.put("note", capture.note);
                    payload.put("expected_audio_hash", capture.audioHash);
                    payload.put("client_capture_id", capture.captureId);
                } catch (JSONException error) {
                    throw new IOException("Cannot encode Tingle metadata update", error);
                }
                byte[] body = payload.toString().getBytes(StandardCharsets.UTF_8);
                connection.setFixedLengthStreamingMode(body.length);
                try (OutputStream output = connection.getOutputStream()) {
                    output.write(body);
                }
                int statusCode = connection.getResponseCode();
                String responseText = readResponse(connection, statusCode);
                if (statusCode < 200 || statusCode >= 300) {
                    throw new UploadFailure(
                            classifyHttpStatus(statusCode),
                            "Click Runtime 返回 " + statusCode + ": " + compactResponse(responseText)
                    );
                }
                return parseReceipt(responseText, capture.audioHash);
            } catch (SocketTimeoutException error) {
                throw new UploadFailure(FailureKind.RETRYABLE, "连接 Click Runtime 超时", error);
            } finally {
                connection.disconnect();
            }
        }

        private static String urlSegment(String value) throws IOException {
            String clean = value == null ? "" : value.trim();
            if (!clean.matches("[A-Za-z0-9._-]{1,200}")) {
                throw new IOException("Invalid recording_id");
            }
            return clean;
        }

        private static void writeRequestBody(
                HttpURLConnection connection,
                TingleCaptureRepository.Capture capture,
                String deviceId
        ) throws IOException {
            JSONObject metadata = new JSONObject();
            try {
                metadata.put("mime_type", "audio/wav");
                metadata.put("duration_seconds", capture.durationSeconds);
                metadata.put("client_capture_id", capture.captureId);
                metadata.put("device_id", deviceId);
                metadata.put("source", "click_android_native_tingle");
                metadata.put("source_app", "Click");
                metadata.put("source_feature", "Tingle");
                metadata.put("durability", "durable");

                JSONObject localMetadata = new JSONObject();
                localMetadata.put("schema", "click.tingle.local_metadata.v1");
                localMetadata.put("title", capture.title);
                localMetadata.put("note", capture.note);
                metadata.put("contexts", new JSONArray().put(localMetadata));
            } catch (JSONException error) {
                throw new IOException("Cannot encode Tingle upload metadata", error);
            }

            String metadataJson = metadata.toString();
            byte[] prefix = "{\"audio_base64\":\"".getBytes(StandardCharsets.UTF_8);
            byte[] suffix = metadataSuffix(metadataJson).getBytes(StandardCharsets.UTF_8);

            try (OutputStream output = connection.getOutputStream()) {
                output.write(prefix);
                Base64OutputStream encoded = new Base64OutputStream(
                        output,
                        Base64.NO_WRAP | Base64.NO_CLOSE
                );
                try (InputStream input = new FileInputStream(capture.audioFile)) {
                    byte[] buffer = new byte[64 * 1024];
                    int read;
                    while ((read = input.read(buffer)) >= 0) {
                        encoded.write(buffer, 0, read);
                    }
                }
                encoded.close();
                output.write(suffix);
                output.flush();
            }
        }

        static String metadataSuffix(String metadataJson) {
            if (metadataJson == null || metadataJson.length() < 2
                    || metadataJson.charAt(0) != '{'
                    || metadataJson.charAt(metadataJson.length() - 1) != '}') {
                throw new IllegalArgumentException("Tingle upload metadata must be a JSON object");
            }
            // Close the audio_base64 JSON string, add a comma, and reuse the metadata object's
            // contents plus its closing brace.
            return "\"," + metadataJson.substring(1);
        }

        private static String readResponse(
                HttpURLConnection connection,
                int statusCode
        ) throws IOException {
            InputStream stream = statusCode >= 200 && statusCode < 400
                    ? connection.getInputStream()
                    : connection.getErrorStream();
            if (stream == null) {
                return "";
            }
            try (InputStream input = stream;
                 ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[8192];
                int total = 0;
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    total += read;
                    if (total > RESPONSE_LIMIT_BYTES) {
                        throw new IOException("Click Runtime response is too large");
                    }
                    output.write(buffer, 0, read);
                }
                return output.toString(StandardCharsets.UTF_8.name());
            }
        }

        private static ServerReceipt parseReceipt(
                String responseText,
                String localAudioHash
        ) throws UploadFailure {
            try {
                JSONObject response = new JSONObject(responseText);
                JSONObject recording = response.optJSONObject("recording");
                String recordingId = recording == null
                        ? ""
                        : recording.optString("recording_id", "").trim();
                String audioHash = recording == null
                        ? ""
                        : recording.optString("audio_hash", "").trim().toLowerCase();
                if (!response.optBoolean("ok", false)
                        || recordingId.isEmpty()
                        || audioHash.isEmpty()) {
                    throw new UploadFailure(
                            FailureKind.RETRYABLE,
                            "Click Runtime 未返回完整录音回执"
                    );
                }
                if (!audioHash.equalsIgnoreCase(localAudioHash)) {
                    throw new UploadFailure(
                            FailureKind.PERMANENT,
                            "Click Runtime 返回的原始音频哈希不一致"
                    );
                }
                return new ServerReceipt(
                        recordingId,
                        audioHash,
                        response.optBoolean("duplicate", false)
                );
            } catch (UploadFailure error) {
                throw error;
            } catch (Throwable error) {
                throw new UploadFailure(
                        FailureKind.RETRYABLE,
                        "Click Runtime 返回了无效录音回执",
                        error
                );
            }
        }
    }

    enum FailureKind {
        RETRYABLE,
        AUTH_REQUIRED,
        PERMANENT
    }

    static final class UploadFailure extends Exception {
        final FailureKind kind;

        UploadFailure(FailureKind kind, String message) {
            super(message);
            this.kind = kind;
        }

        UploadFailure(FailureKind kind, String message, Throwable cause) {
            super(message, cause);
            this.kind = kind;
        }
    }

    static final class ServerReceipt {
        final String recordingId;
        final String audioHash;
        final boolean duplicate;

        ServerReceipt(String recordingId, String audioHash, boolean duplicate) {
            this.recordingId = recordingId;
            this.audioHash = audioHash;
            this.duplicate = duplicate;
        }
    }

    static final class Outcome {
        enum Status {
            SUCCESS,
            BUSY,
            RETRYABLE_FAILURE,
            AUTH_REQUIRED,
            PERMANENT_FAILURE
        }

        final Status status;
        final int uploaded;
        final int permanentlyFailed;
        final int pending;
        final String message;

        private Outcome(
                Status status,
                int uploaded,
                int permanentlyFailed,
                int pending,
                String message
        ) {
            this.status = status;
            this.uploaded = uploaded;
            this.permanentlyFailed = permanentlyFailed;
            this.pending = pending;
            this.message = message;
        }

        static Outcome of(
                Status status,
                int uploaded,
                int permanentlyFailed,
                int pending,
                String message
        ) {
            return new Outcome(
                    status,
                    Math.max(0, uploaded),
                    Math.max(0, permanentlyFailed),
                    Math.max(0, pending),
                    message == null ? "" : message.trim()
            );
        }
    }

    private static String compact(Throwable error) {
        if (error == null) {
            return "Tingle upload failed";
        }
        String message = error.getMessage();
        if (message == null || message.trim().isEmpty()) {
            message = error.getClass().getSimpleName();
        }
        return compactResponse(message);
    }

    private static String compactResponse(String value) {
        String clean = value == null ? "" : value.trim().replaceAll("\\s+", " ");
        return clean.length() <= 500 ? clean : clean.substring(0, 500);
    }
}
