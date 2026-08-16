package com.click.shell;

import android.content.Context;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.Collections;

/**
 * Durable, one-original-per-run Android book import.
 *
 * <p>The queue and original file remain authoritative until a verified canonical receipt is
 * committed by {@link ClickNativeStore}. Network failures never delete or rename the original.</p>
 */
final class BookImportRepository implements AutoCloseable {
    private static final int CONNECT_TIMEOUT_MILLIS = 15_000;
    private static final int READ_TIMEOUT_MILLIS = 180_000;
    private static final int RESPONSE_LIMIT_BYTES = 2 * 1024 * 1024;

    private final Context appContext;
    private final ClickNativeStore store;
    private final Transport transport;

    BookImportRepository(Context context) {
        this(context, new ClickNativeStore(context.getApplicationContext()), new HttpTransport());
    }

    BookImportRepository(Context context, ClickNativeStore store, Transport transport) {
        this.appContext = context.getApplicationContext();
        this.store = store;
        this.transport = transport;
    }

    static void registerLocalOriginal(
            Context context,
            String localBookId,
            String title,
            String author,
            String sourceKind,
            String sourceLocalPath,
            String sourceHash,
            long sourceByteSize,
            String filename
    ) throws Exception {
        Context appContext = context.getApplicationContext();
        File source = new File(sourceLocalPath);
        try (ClickNativeStore store = new ClickNativeStore(appContext)) {
            store.registerLocalBookImport(
                    localBookId,
                    title,
                    author,
                    sourceKind,
                    sourceLocalPath,
                    sourceHash,
                    sourceByteSize,
                    filename,
                    "epub".equals(sourceKind)
                            ? EpubOfflineIndexer.index(source)
                            : Collections.emptyList()
            );
        }
        BookImportScheduler.enqueue(appContext);
    }

    Outcome uploadOne() {
        ClickNativeStore.BookImportRow queued = store.nextPendingBookImport();
        if (queued == null) {
            return Outcome.of(Status.SUCCESS, false, 0, "没有待导入原书");
        }

        // Load the current encrypted credential only when WorkManager actually executes this run.
        ClickSyncConfig config = ClickSyncConfig.load(appContext);
        if (!config.isReady()) {
            store.markBookImportFailed(queued.localBookId, "Click 尚未完成设备授权", false);
            return Outcome.of(
                    Status.AUTH_REQUIRED,
                    false,
                    store.pendingBookImportCount(),
                    "Click 尚未完成设备授权"
            );
        }

        try {
            Receipt receipt = transport.upload(queued, config);
            if (!queued.sourceHash.equals(receipt.fileHash)
                    || !queued.sourceKind.equals(receipt.sourceKind)
                    || queued.sourceByteSize != receipt.byteSize) {
                throw new UploadFailure(
                        FailureKind.PERMANENT,
                        "服务端导入回执与本地原书不一致"
                );
            }
            store.completeBookImport(
                    queued.localBookId,
                    queued.sourceHash,
                    receipt.canonicalBookId
            );
            return Outcome.of(
                    Status.SUCCESS,
                    true,
                    store.pendingBookImportCount(),
                    receipt.duplicate ? "原书已合并到既有书籍" : "原书已导入"
            );
        } catch (UploadFailure error) {
            boolean terminal = error.kind == FailureKind.PERMANENT;
            store.markBookImportFailed(queued.localBookId, compact(error), terminal);
            return Outcome.of(
                    error.kind == FailureKind.AUTH_REQUIRED
                            ? Status.AUTH_REQUIRED
                            : terminal ? Status.PERMANENT_FAILURE : Status.RETRYABLE_FAILURE,
                    false,
                    store.pendingBookImportCount(),
                    compact(error)
            );
        } catch (IOException error) {
            store.markBookImportFailed(queued.localBookId, compact(error), false);
            return Outcome.of(
                    Status.RETRYABLE_FAILURE,
                    false,
                    store.pendingBookImportCount(),
                    compact(error)
            );
        } catch (Throwable error) {
            store.markBookImportFailed(queued.localBookId, compact(error), true);
            return Outcome.of(
                    Status.PERMANENT_FAILURE,
                    false,
                    store.pendingBookImportCount(),
                    compact(error)
            );
        }
    }

    static FailureKind classifyHttpStatus(int statusCode) {
        if (statusCode == 401 || statusCode == 403) {
            return FailureKind.AUTH_REQUIRED;
        }
        if (statusCode == 408
                || statusCode == 425
                || statusCode == 429
                || statusCode >= 500) {
            return FailureKind.RETRYABLE;
        }
        return FailureKind.PERMANENT;
    }

    private static String compact(Throwable error) {
        String message = error == null || error.getMessage() == null
                ? "原书导入失败"
                : error.getMessage().trim();
        return message.length() <= 240 ? message : message.substring(0, 240);
    }

    @Override
    public void close() {
        store.close();
    }

    interface Transport {
        Receipt upload(ClickNativeStore.BookImportRow row, ClickSyncConfig config)
                throws IOException, UploadFailure;
    }

    static final class HttpTransport implements Transport {
        @Override
        public Receipt upload(ClickNativeStore.BookImportRow row, ClickSyncConfig config)
                throws IOException, UploadFailure {
            File source = new File(row.sourceLocalPath);
            if (!source.isFile()
                    || source.length() != row.sourceByteSize
                    || row.sourceByteSize <= 0L) {
                throw new UploadFailure(FailureKind.PERMANENT, "本地原书不存在或大小已变化");
            }
            String endpoint = ReaderApiUrlPolicy.resolve(
                    config.baseUrl,
                    "/v1/android/imports/" + row.sourceHash
            );
            HttpURLConnection connection =
                    (HttpURLConnection) new URL(endpoint).openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setConnectTimeout(CONNECT_TIMEOUT_MILLIS);
            connection.setReadTimeout(READ_TIMEOUT_MILLIS);
            connection.setRequestMethod("PUT");
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(row.sourceByteSize);
            connection.setRequestProperty("Content-Type", "application/octet-stream");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("X-Click-Device-Id", config.deviceId);
            connection.setRequestProperty("X-Click-Access-Token", config.accessToken);
            connection.setRequestProperty("Authorization", "Bearer " + config.accessToken);
            connection.setRequestProperty(
                    "X-Click-Filename",
                    URLEncoder.encode(row.filename, StandardCharsets.UTF_8.name())
                            .replace("+", "%20")
            );
            connection.setRequestProperty("X-Click-Source-Kind", row.sourceKind);
            connection.setRequestProperty(
                    "X-Click-Byte-Size",
                    String.valueOf(row.sourceByteSize)
            );

            try {
                String streamedHash = streamSource(connection, source);
                if (!row.sourceHash.equals(streamedHash)) {
                    throw new UploadFailure(
                            FailureKind.PERMANENT,
                            "本地原书 SHA-256 已变化"
                    );
                }
                int statusCode = connection.getResponseCode();
                String responseText = readResponse(connection, statusCode);
                if (statusCode < 200 || statusCode >= 300) {
                    throw new UploadFailure(
                            classifyHttpStatus(statusCode),
                            "Click Runtime 返回 " + statusCode + ": " + responseText
                    );
                }
                return parseReceipt(responseText);
            } finally {
                connection.disconnect();
            }
        }

        private String streamSource(HttpURLConnection connection, File source)
                throws IOException, UploadFailure {
            try {
                MessageDigest digest = MessageDigest.getInstance("SHA-256");
                try (InputStream input = new FileInputStream(source);
                     OutputStream output = connection.getOutputStream()) {
                    byte[] buffer = new byte[64 * 1024];
                    int read;
                    while ((read = input.read(buffer)) >= 0) {
                        if (Thread.currentThread().isInterrupted()) {
                            throw new InterruptedIOException("原书上传已停止");
                        }
                        if (read > 0) {
                            digest.update(buffer, 0, read);
                            output.write(buffer, 0, read);
                        }
                    }
                    output.flush();
                }
                return hex(digest.digest());
            } catch (IOException error) {
                throw error;
            } catch (Throwable error) {
                throw new UploadFailure(FailureKind.PERMANENT, "无法校验本地原书", error);
            }
        }

        private String readResponse(HttpURLConnection connection, int statusCode)
                throws IOException {
            InputStream raw = statusCode >= 200 && statusCode < 300
                    ? connection.getInputStream()
                    : connection.getErrorStream();
            if (raw == null) {
                return "";
            }
            try (InputStream input = raw;
                 ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[8192];
                int total = 0;
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    total += read;
                    if (total > RESPONSE_LIMIT_BYTES) {
                        throw new IOException("原书导入响应过大");
                    }
                    output.write(buffer, 0, read);
                }
                return output.toString(StandardCharsets.UTF_8.name());
            }
        }

        private Receipt parseReceipt(String responseText) throws UploadFailure {
            try {
                JSONObject payload = new JSONObject(responseText);
                String bookId = payload.optString("book_id").trim();
                String sourceKind = payload.optString("source_kind")
                        .trim()
                        .toLowerCase(Locale.ROOT);
                String fileHash = payload.optString("file_hash")
                        .trim()
                        .toLowerCase(Locale.ROOT);
                long byteSize = payload.optLong("byte_size", -1L);
                if (!payload.optBoolean("ok")
                        || bookId.isEmpty()
                        || (!"epub".equals(sourceKind) && !"pdf".equals(sourceKind))
                        || !fileHash.matches("[0-9a-f]{64}")
                        || byteSize <= 0L) {
                    throw new UploadFailure(
                            FailureKind.PERMANENT,
                            "服务端原书导入回执不完整"
                    );
                }
                return new Receipt(
                        bookId,
                        sourceKind,
                        fileHash,
                        byteSize,
                        payload.optBoolean("duplicate")
                );
            } catch (UploadFailure error) {
                throw error;
            } catch (Throwable error) {
                throw new UploadFailure(
                        FailureKind.PERMANENT,
                        "无法解析服务端原书导入回执",
                        error
                );
            }
        }

        private String hex(byte[] digest) {
            StringBuilder value = new StringBuilder();
            for (byte item : digest) {
                value.append(String.format(Locale.ROOT, "%02x", item & 0xff));
            }
            return value.toString();
        }
    }

    enum FailureKind {
        RETRYABLE,
        AUTH_REQUIRED,
        PERMANENT
    }

    enum Status {
        SUCCESS,
        RETRYABLE_FAILURE,
        AUTH_REQUIRED,
        PERMANENT_FAILURE
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

    static final class Receipt {
        final String canonicalBookId;
        final String sourceKind;
        final String fileHash;
        final long byteSize;
        final boolean duplicate;

        Receipt(
                String canonicalBookId,
                String sourceKind,
                String fileHash,
                long byteSize,
                boolean duplicate
        ) {
            this.canonicalBookId = canonicalBookId;
            this.sourceKind = sourceKind;
            this.fileHash = fileHash;
            this.byteSize = byteSize;
            this.duplicate = duplicate;
        }
    }

    static final class Outcome {
        final Status status;
        final boolean uploaded;
        final int pending;
        final String message;

        private Outcome(Status status, boolean uploaded, int pending, String message) {
            this.status = status;
            this.uploaded = uploaded;
            this.pending = Math.max(0, pending);
            this.message = message == null ? "" : message.trim();
        }

        static Outcome of(Status status, boolean uploaded, int pending, String message) {
            return new Outcome(status, uploaded, pending, message);
        }
    }
}
