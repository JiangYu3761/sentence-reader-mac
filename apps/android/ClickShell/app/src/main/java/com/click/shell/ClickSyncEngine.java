package com.click.shell;

import android.content.Context;
import android.net.Uri;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InterruptedIOException;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * The single Android sync protocol implementation shared by foreground UI and WorkManager.
 *
 * <p>This class is deliberately headless: it owns no Activity, View, Toast, WebView, Intent,
 * notification or lifecycle object.</p>
 */
final class ClickSyncEngine implements AutoCloseable {
    static final String FULL_BASELINE_PATH =
            "/v1/android/sync/full?include_chapters=false";
    private static final int MAX_OPERATION_UPLOAD_BATCHES = 10;
    private static final int MAX_CHANGE_PAGES = 20;
    private static final int CHANGE_PAGE_SIZE = 100;
    private static final int DEFAULT_MAX_ASSETS = 1;

    private final Context appContext;
    private final ClickSyncConfig config;
    private final ClickNativeStore store;
    private final Transport transport;
    private final Clock clock;

    ClickSyncEngine(Context context) {
        this.appContext = context.getApplicationContext();
        this.config = ClickSyncConfig.load(appContext);
        this.store = new ClickNativeStore(appContext);
        this.transport = new HttpTransport(config);
        this.clock = System::currentTimeMillis;
    }

    Outcome runFullForeground(ProgressSink progressSink) {
        ProgressSink progress = progressSink == null ? ProgressSink.NONE : progressSink;
        return runExclusive(() -> runFullBaseline(progress));
    }

    Outcome runMetadataIncremental() {
        return runExclusive(() -> {
            if (shouldRunFullBaseline(store.hasFullBaseline())) {
                return runFullBaseline(ProgressSink.NONE);
            }
            MutableStats stats = new MutableStats();
            UploadResult upload = uploadPendingOperations(stats);

            long afterSequence = Math.max(0L, store.syncSequence());
            boolean caughtUp = false;
            for (int pageIndex = 0; pageIndex < MAX_CHANGE_PAGES; pageIndex++) {
                JSONObject payload = transport.requestJson(
                        "GET",
                        "/v1/android/sync/changes?after_sequence="
                                + afterSequence
                                + "&limit="
                                + CHANGE_PAGE_SIZE,
                        null
                );
                requireSuccessfulPayload(payload, "增量同步");
                if (!payload.has("next_sequence")) {
                    throw new SyncContractException("增量同步缺少 next_sequence");
                }
                long nextSequence = payload.optLong("next_sequence", -1L);
                if (nextSequence < afterSequence) {
                    throw new SyncContractException("增量同步顺序游标倒退");
                }
                List<String> removedLocalFiles = removedLocalFilePaths(payload);
                store.saveChanges(payload);
                deleteAppPrivateFiles(removedLocalFiles);
                JSONArray events = payload.optJSONArray("events");
                stats.changesApplied += events == null ? 0 : events.length();

                boolean hasMore = payload.optBoolean("has_more", false);
                if (!hasMore) {
                    caughtUp = true;
                    break;
                }
                if (nextSequence == afterSequence) {
                    throw new SyncContractException("增量同步分页未向前推进");
                }
                afterSequence = nextSequence;
            }
            if (!caughtUp) {
                throw new RetryableSyncException("本轮变更达到分页上限");
            }
            Outcome.Status repairStatus = repairOneMetadataBook(stats);
            if (repairStatus == Outcome.Status.AUTH_REQUIRED) {
                return outcome(repairStatus, "Mac 授权已失效", stats);
            }
            return outcome(
                    upload.retryableOperationRemains
                            ? Outcome.Status.RETRYABLE_FAILURE
                            : Outcome.Status.SUCCESS,
                    upload.retryableOperationRemains
                            ? "远端变化已拉取；仍有手机端操作等待重试"
                            : "小同步完成",
                    stats
            );
        });
    }

    Outcome runMetadataRepairOnly() {
        return runExclusive(() -> {
            MutableStats stats = new MutableStats();
            Outcome.Status status = repairOneMetadataBook(stats);
            return outcome(
                    status,
                    status == Outcome.Status.AUTH_REQUIRED
                            ? "Mac 授权已失效"
                            : stats.metadataRepaired > 0
                            ? "已修复 1 本书的元数据"
                            : stats.metadataTerminalFailures > 0
                            ? "1 本书的元数据修复已隔离，等待书源变化"
                            : stats.metadataDeferred > 0
                            ? "书籍元数据修复已按退避时间延期"
                            : "没有到期的书籍元数据修复",
                    stats
            );
        });
    }

    Outcome runAssetOnly(int requestedMaxItems) {
        int maxItems = Math.max(1, Math.min(DEFAULT_MAX_ASSETS, requestedMaxItems));
        return runExclusive(() -> {
            List<ClickNativeStore.AssetRefreshRow> due = store.dueAssetRefreshes(
                    maxItems,
                    clock.nowMillis()
            );
            return runAssetRows(due);
        });
    }

    Outcome runAssetOnly() {
        return runAssetOnly(DEFAULT_MAX_ASSETS);
    }

    Outcome runBookSourceAssetOnly(String bookId) {
        String cleanBookId = clean(bookId);
        return runExclusive(() -> {
            if (cleanBookId.isEmpty()) {
                throw new SyncContractException("单本原书下载缺少 book_id");
            }
            List<ClickNativeStore.AssetRefreshRow> due = new ArrayList<>();
            ClickNativeStore.AssetRefreshRow row = store.dueBookSourceRefresh(
                    cleanBookId,
                    clock.nowMillis()
            );
            if (row != null) {
                if (!isRequestedBookSource(row, cleanBookId)) {
                    throw new SyncContractException("单本原书下载越过指定书籍");
                }
                due.add(row);
            }
            return runAssetRows(due);
        });
    }

    static boolean isRequestedBookSource(
            ClickNativeStore.AssetRefreshRow row,
            String requestedBookId
    ) {
        return row != null
                && ClickNativeStore.ASSET_SOURCE.equals(row.assetType)
                && row.bookId.equals(clean(requestedBookId));
    }

    private Outcome runAssetRows(List<ClickNativeStore.AssetRefreshRow> due) {
        MutableStats stats = new MutableStats();
        for (ClickNativeStore.AssetRefreshRow queued : due) {
            ClickNativeStore.AssetRefreshRow current = queued;
            JSONObject verifiedPublication = null;
            try {
                if (ClickNativeStore.ASSET_EPUB.equals(queued.assetType)) {
                    verifiedPublication = transport.requestJson(
                            "GET",
                            "/v1/android/books/"
                                    + Uri.encode(queued.bookId)
                                    + "/publication?include_chapters=true",
                            null
                    );
                    requireSuccessfulPayload(verifiedPublication, "刷新 EPUB 与章节");
                    current = epubContractFromPublication(verifiedPublication, queued);
                }
                if (ClickNativeStore.ASSET_SOURCE.equals(current.assetType)
                        && (!clean(current.expectedHash).matches("[0-9a-fA-F]{64}")
                        || current.expectedByteSize <= 0L)) {
                    throw new SyncContractException("原书资源缺少 SHA-256 或字节数");
                }
                File target = assetTarget(current);
                transport.download(
                        current.assetUrl,
                        target,
                        requiresVerifiedContent(current.assetType)
                                ? current.expectedHash
                                : "",
                        requiresVerifiedContent(current.assetType)
                                ? current.expectedByteSize
                                : -1L
                );
                if (verifiedPublication != null) {
                    JSONObject oneBook = new JSONObject()
                            .put("books", new JSONArray().put(verifiedPublication));
                    List<String> clearedDisplayVariants =
                            displayVariantLocalFilesClearedBy(oneBook);
                    store.commitVerifiedEpubRefresh(
                            verifiedPublication,
                            target.getAbsolutePath()
                    );
                    deleteAppPrivateFiles(clearedDisplayVariants);
                } else {
                    store.markAssetRefreshSucceeded(
                            current.bookId,
                            current.assetType,
                            target.getAbsolutePath()
                    );
                }
                stats.assetsDownloaded += 1;
            } catch (Throwable error) {
                Outcome.Status status = classify(error);
                if (status == Outcome.Status.RETRYABLE_FAILURE) {
                    store.markAssetRefreshFailed(
                            queued.bookId,
                            queued.assetType,
                            assetFailureMessage(queued.assetType, error),
                            clock.nowMillis()
                    );
                    stats.assetsDeferred += 1;
                    continue;
                }
                if (status == Outcome.Status.AUTH_REQUIRED) {
                    // Authentication is global, not an asset defect. Leave the row pending.
                    return outcome(status, compact(error), stats);
                }
                store.markAssetRefreshTerminal(
                        queued.bookId,
                        queued.assetType,
                        assetFailureMessage(queued.assetType, error)
                );
                stats.assetTerminalFailures += 1;
            }
        }
        return outcome(
                Outcome.Status.SUCCESS,
                stats.assetTerminalFailures > 0
                        ? "资源缓存异常已隔离，其他资源会继续"
                        : stats.assetsDeferred > 0
                        ? "资源已按退避时间延期"
                        : "资源同步完成",
                stats
        );
    }

    private Outcome runFullBaseline(ProgressSink progress) throws Exception {
        MutableStats stats = new MutableStats();
        progress.onStage("upload_operations");
        UploadResult upload = uploadPendingOperations(stats);

        progress.onStage("download_full_metadata");
        JSONObject payload = transport.requestJson(
                "GET",
                FULL_BASELINE_PATH,
                null
        );
        requireSuccessfulFullPayload(payload);
        if (!payload.has("watermark_sequence")) {
            throw new SyncContractException("全量同步缺少 watermark_sequence");
        }
        JSONArray fullErrors = payload.optJSONArray("errors");
        stats.booksFailed = Math.max(
                0,
                payload.optInt(
                        "error_count",
                        fullErrors == null ? 0 : fullErrors.length()
                )
        );

        progress.onStage("commit_full_baseline");
        List<String> removedLocalFiles = removedLocalFilePaths(payload);
        removedLocalFiles.addAll(displayVariantLocalFilesClearedBy(payload));
        // saveFullBaseline must persist books, the asset queue, watermark_sequence and
        // full_sync_completed in one SQLite transaction. Per-book failures enter the durable
        // metadata repair queue instead of forcing another full-library scan.
        store.saveFullBaseline(payload);
        deleteAppPrivateFiles(removedLocalFiles);
        JSONArray books = payload.optJSONArray("books");
        stats.changesApplied += books == null ? 0 : books.length();
        progress.onStage("complete");
        String summary = stats.booksFailed > 0
                ? "全量元数据已建立；" + stats.booksFailed + " 本书进入修复队列"
                : "全量元数据同步完成";
        if (upload.retryableOperationRemains) {
            summary += "；仍有手机端操作等待重试";
        }
        return outcome(
                upload.retryableOperationRemains
                        ? Outcome.Status.RETRYABLE_FAILURE
                        : Outcome.Status.SUCCESS,
                summary,
                stats
        );
    }

    private UploadResult uploadPendingOperations(MutableStats stats) throws Exception {
        for (int batchIndex = 0; batchIndex < MAX_OPERATION_UPLOAD_BATCHES; batchIndex++) {
            JSONArray pending = store.pendingOperations(config.deviceId);
            if (pending.length() == 0) {
                return UploadResult.COMPLETE;
            }
            JSONObject body = new JSONObject()
                    .put("device_id", config.deviceId)
                    .put("operations", pending);
            JSONObject response = transport.requestJson(
                    "POST",
                    "/v1/android/sync/operations",
                    body
            );
            JSONArray results = response.optJSONArray("results");
            if (results == null || results.length() == 0) {
                throw new SyncContractException("操作同步缺少 results");
            }
            if (!operationReceiptIdsMatchExactly(pending, results)) {
                throw new SyncContractException("操作同步回执 operation_id 集合不完整或不唯一");
            }

            boolean retryableRemains = false;
            for (int index = 0; index < results.length(); index++) {
                JSONObject item = results.optJSONObject(index);
                if (item == null || clean(item.optString("operation_id")).isEmpty()) {
                    throw new SyncContractException("操作同步回执缺少 operation_id");
                }
                store.markOperationResult(item);
                if (item.optBoolean("ok", false)) {
                    stats.operationsUploaded += 1;
                } else if (item.optBoolean("retryable", false)) {
                    retryableRemains = true;
                }
                // conflict and permanent_failed are durable business outcomes. The store excludes
                // them from future upload batches, and the run continues to pull remote changes.
            }
            if (retryableRemains) {
                return UploadResult.RETRYABLE_REMAINS;
            }
        }
        return store.pendingOperations(config.deviceId).length() == 0
                ? UploadResult.COMPLETE
                : UploadResult.RETRYABLE_REMAINS;
    }

    static boolean operationReceiptIdsMatchExactly(
            JSONArray pending,
            JSONArray results
    ) {
        if (pending == null || results == null || pending.length() != results.length()) {
            return false;
        }
        Set<String> expected = new HashSet<>();
        Set<String> actual = new HashSet<>();
        for (int index = 0; index < pending.length(); index++) {
            JSONObject operation = pending.optJSONObject(index);
            String operationId = operation == null
                    ? ""
                    : clean(operation.optString("operation_id"));
            if (operationId.isEmpty() || !expected.add(operationId)) {
                return false;
            }
        }
        for (int index = 0; index < results.length(); index++) {
            JSONObject result = results.optJSONObject(index);
            String operationId = result == null
                    ? ""
                    : clean(result.optString("operation_id"));
            if (operationId.isEmpty() || !actual.add(operationId)) {
                return false;
            }
        }
        return expected.equals(actual);
    }

    private Outcome.Status repairOneMetadataBook(MutableStats stats) {
        List<ClickNativeStore.MetadataRepairRow> due = store.dueMetadataRepairs(
                1,
                clock.nowMillis()
        );
        if (due.isEmpty()) {
            return Outcome.Status.SUCCESS;
        }
        ClickNativeStore.MetadataRepairRow repair = due.get(0);
        try {
            JSONObject publication = transport.requestJson(
                    "GET",
                    "/v1/android/books/"
                            + Uri.encode(repair.bookId)
                            + "/publication?include_chapters=false",
                    null
            );
            requireSuccessfulPayload(publication, "修复书籍元数据");
            JSONObject oneBook = new JSONObject()
                    .put("books", new JSONArray().put(publication));
            List<String> clearedDisplayVariants =
                    displayVariantLocalFilesClearedBy(oneBook);
            store.saveFullSync(oneBook);
            deleteAppPrivateFiles(clearedDisplayVariants);
            store.markMetadataRepairSucceeded(repair.bookId);
            stats.metadataRepaired += 1;
            return Outcome.Status.SUCCESS;
        } catch (Throwable error) {
            Outcome.Status status = classify(error);
            if (status == Outcome.Status.AUTH_REQUIRED) {
                // Authentication is not a property of this book. Keep the row actionable so a
                // successful re-pair can resume it without requiring a contract change.
                return status;
            }
            if (status == Outcome.Status.RETRYABLE_FAILURE) {
                store.markMetadataRepairFailed(
                        repair.bookId,
                        "元数据修复失败：" + compact(error),
                        clock.nowMillis()
                );
                stats.metadataDeferred += 1;
                return Outcome.Status.SUCCESS;
            }
            store.markMetadataRepairTerminal(
                    repair.bookId,
                    "元数据修复失败：" + compact(error)
            );
            stats.metadataTerminalFailures += 1;
            return Outcome.Status.SUCCESS;
        }
    }

    private Outcome runExclusive(ExclusiveWork work) {
        ClickSyncRunGate.Lease lease = ClickSyncRunGate.tryAcquire();
        if (lease == null) {
            return Outcome.simple(Outcome.Status.BUSY, "已有同步正在运行");
        }
        try (ClickSyncRunGate.Lease ignored = lease) {
            if (!config.isReady()) {
                return Outcome.simple(Outcome.Status.NOT_CONFIGURED, "尚未完成同步配对");
            }
            return work.run();
        } catch (Throwable error) {
            return Outcome.simple(classify(error), compact(error));
        }
    }

    private Outcome outcome(Outcome.Status status, String message, MutableStats stats) {
        AssetState assetState = assetState();
        MetadataRepairState metadataRepairState = metadataRepairState();
        boolean pendingOperationsRemain;
        try {
            pendingOperationsRemain = store.hasPendingOperations();
        } catch (Throwable ignored) {
            // Preserve a wake-up if the final durable tail check itself cannot be read.
            pendingOperationsRemain = true;
        }
        return new Outcome(
                status,
                clean(message),
                stats.operationsUploaded,
                stats.changesApplied,
                stats.assetsDownloaded,
                stats.assetsDeferred,
                stats.booksFailed,
                stats.assetTerminalFailures,
                stats.metadataRepaired,
                stats.metadataDeferred,
                stats.metadataTerminalFailures,
                assetState.any,
                assetState.dueNow,
                assetState.nextAttemptAtMillis,
                metadataRepairState.any,
                metadataRepairState.dueNow,
                metadataRepairState.nextAttemptAtMillis,
                pendingOperationsRemain
        );
    }

    private AssetState assetState() {
        try {
            if (!store.hasAssetRefreshWork()) {
                return AssetState.EMPTY;
            }
            long now = clock.nowMillis();
            boolean dueNow = !store.dueAssetRefreshes(1, now).isEmpty();
            long nextAt = store.earliestAssetAttemptAt();
            return new AssetState(true, dueNow, nextAt);
        } catch (Throwable ignored) {
            // Unknown is represented as remaining work without a timestamp. The worker maps that
            // invariant breach to retry instead of reporting a false terminal success.
            return AssetState.UNKNOWN;
        }
    }

    private MetadataRepairState metadataRepairState() {
        try {
            if (!store.hasMetadataRepairWork()) {
                return MetadataRepairState.EMPTY;
            }
            long now = clock.nowMillis();
            boolean dueNow = !store.dueMetadataRepairs(1, now).isEmpty();
            return new MetadataRepairState(
                    true,
                    dueNow,
                    store.earliestMetadataRepairAttemptAt()
            );
        } catch (Throwable ignored) {
            return MetadataRepairState.UNKNOWN;
        }
    }

    private void requireSuccessfulPayload(JSONObject payload, String operation)
            throws SyncContractException {
        if (payload.optBoolean("ok", false)) {
            return;
        }
        String detail = clean(payload.optString("error"));
        JSONArray errors = payload.optJSONArray("errors");
        if (detail.isEmpty() && errors != null && errors.length() > 0) {
            JSONObject first = errors.optJSONObject(0);
            detail = first == null
                    ? clean(errors.optString(0))
                    : clean(first.optString("error", first.optString("detail")));
        }
        throw new SyncContractException(
                operation + "失败：" + (detail.isEmpty() ? "服务端返回失败状态" : detail)
        );
    }

    private void requireSuccessfulFullPayload(JSONObject payload)
            throws SyncContractException {
        requireSuccessfulPayload(payload, "全量同步");
    }

    private List<String> removedLocalFilePaths(JSONObject payload) {
        List<String> paths = new ArrayList<>();
        JSONArray removed = payload.optJSONArray("removed_book_ids");
        if (removed == null) {
            return paths;
        }
        for (int index = 0; index < removed.length(); index++) {
            String bookId = clean(removed.optString(index));
            if (bookId.isEmpty()) {
                continue;
            }
            paths.add(store.bookEpubLocalPath(bookId));
            paths.add(store.bookCoverLocalPath(bookId));
            paths.add(store.bookDisplayVariantsLocalPath(bookId));
        }
        return paths;
    }

    private List<String> displayVariantLocalFilesClearedBy(JSONObject payload) {
        List<String> paths = new ArrayList<>();
        JSONArray books = payload.optJSONArray("books");
        if (books == null) {
            return paths;
        }
        for (int index = 0; index < books.length(); index++) {
            JSONObject item = books.optJSONObject(index);
            JSONObject book = item == null ? null : item.optJSONObject("book");
            if (book == null) {
                continue;
            }
            JSONObject variants = item.optJSONObject("display_variants");
            boolean available = variants != null
                    && variants.optBoolean("available")
                    && !clean(variants.optString("url")).isEmpty();
            if (!available) {
                paths.add(store.bookDisplayVariantsLocalPath(book.optString("id")));
            }
        }
        return paths;
    }

    private void deleteAppPrivateFiles(List<String> paths) {
        for (String path : paths) {
            deleteAppPrivateFile(path);
        }
    }

    private void deleteAppPrivateFile(String rawPath) {
        String path = clean(rawPath);
        if (path.isEmpty()) {
            return;
        }
        try {
            File root = appContext.getFilesDir().getCanonicalFile();
            File target = new File(path).getCanonicalFile();
            String rootPrefix = root.getPath() + File.separator;
            if (target.getPath().startsWith(rootPrefix) && target.isFile()) {
                //noinspection ResultOfMethodCallIgnored
                target.delete();
            }
        } catch (Throwable ignored) {
            // Never delete an unverified path and never make cache cleanup block metadata sync.
        }
    }

    private File assetTarget(ClickNativeStore.AssetRefreshRow row) throws IOException {
        String directoryName;
        String suffix;
        if (ClickNativeStore.ASSET_EPUB.equals(row.assetType)) {
            directoryName = "click-epub-cache";
            suffix = ".epub";
        } else if (ClickNativeStore.ASSET_SOURCE.equals(row.assetType)) {
            directoryName = "click-source-library";
            String sourceKind = clean(store.bookSourceKind(row.bookId))
                    .toLowerCase(java.util.Locale.ROOT);
            if (!"epub".equals(sourceKind) && !"pdf".equals(sourceKind)) {
                throw new IOException("原书资源类型无效");
            }
            suffix = "." + sourceKind;
        } else if (ClickNativeStore.ASSET_DISPLAY_VARIANTS.equals(row.assetType)) {
            directoryName = "click-display-variant-cache";
            suffix = ".json";
        } else {
            directoryName = "click-cover-cache";
            suffix = ".cover";
        }
        File directory = new File(appContext.getFilesDir(), directoryName);
        if (!directory.isDirectory() && !directory.mkdirs() && !directory.isDirectory()) {
            throw new IOException("无法创建资源缓存目录");
        }
        File canonicalDirectory = directory.getCanonicalFile();
        File target = new File(
                canonicalDirectory,
                safeAssetFileStem(row.bookId) + suffix
        ).getCanonicalFile();
        if (!target.getPath().startsWith(canonicalDirectory.getPath() + File.separator)) {
            throw new IOException("资源文件名越过缓存目录");
        }
        return target;
    }

    private ClickNativeStore.AssetRefreshRow epubContractFromPublication(
            JSONObject publication,
            ClickNativeStore.AssetRefreshRow queued
    ) throws SyncContractException {
        JSONObject book = publication.optJSONObject("book");
        JSONObject epub = publication.optJSONObject("epub");
        JSONArray chapters = publication.optJSONArray("chapters");
        String bookId = book == null ? "" : clean(book.optString("id"));
        String assetUrl = epub == null ? "" : clean(epub.optString("url"));
        String expectedHash = epub == null ? "" : clean(epub.optString("file_hash"));
        long expectedBytes = epub == null ? -1L : epub.optLong("byte_size", -1L);
        if (!queued.bookId.equals(bookId)
                || assetUrl.isEmpty()
                || expectedHash.isEmpty()
                || expectedBytes <= 0L
                || chapters == null) {
            throw new SyncContractException("EPUB 刷新合同不完整");
        }
        return new ClickNativeStore.AssetRefreshRow(
                bookId,
                ClickNativeStore.ASSET_EPUB,
                assetUrl,
                expectedHash,
                expectedBytes,
                expectedHash + ":" + expectedBytes,
                "pending",
                queued.retryCount,
                "",
                0L
        );
    }

    static String safeAssetFileStem(String bookId) throws IOException {
        String cleanId = clean(bookId);
        if (cleanId.isEmpty()) {
            throw new IOException("资源缺少 book_id");
        }
        if (cleanId.matches("[A-Za-z0-9][A-Za-z0-9._-]{0,119}")) {
            return cleanId;
        }
        String readable = cleanId.replaceAll("[^A-Za-z0-9._-]", "_");
        readable = readable.replaceFirst("^[._-]+", "");
        if (readable.isEmpty()) {
            readable = "book";
        }
        if (readable.length() > 48) {
            readable = readable.substring(0, 48);
        }
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(cleanId.getBytes(StandardCharsets.UTF_8));
            StringBuilder suffix = new StringBuilder();
            for (int index = 0; index < 8; index++) {
                suffix.append(String.format(java.util.Locale.ROOT, "%02x", digest[index]));
            }
            return readable + "-" + suffix;
        } catch (Throwable error) {
            throw new IOException("无法生成安全资源文件名", error);
        }
    }

    private String assetFailureMessage(String assetType, Throwable error) {
        String label = ClickNativeStore.ASSET_EPUB.equals(assetType)
                ? "EPUB"
                : ClickNativeStore.ASSET_DISPLAY_VARIANTS.equals(assetType)
                ? "简繁显示文件"
                : ClickNativeStore.ASSET_SOURCE.equals(assetType)
                ? "原书"
                : "封面";
        return label + "缓存失败：" + compact(error);
    }

    private static boolean requiresVerifiedContent(String assetType) {
        return ClickNativeStore.ASSET_EPUB.equals(assetType)
                || ClickNativeStore.ASSET_SOURCE.equals(assetType);
    }

    static Outcome.Status classifyHttpStatus(int statusCode) {
        if (statusCode == 401 || statusCode == 403) {
            return Outcome.Status.AUTH_REQUIRED;
        }
        if (statusCode == 408
                || statusCode == 425
                || statusCode == 429
                || statusCode >= 500) {
            return Outcome.Status.RETRYABLE_FAILURE;
        }
        return Outcome.Status.PERMANENT_FAILURE;
    }

    static boolean shouldRunFullBaseline(boolean hasFullBaseline) {
        return !hasFullBaseline;
    }

    static Outcome.Status classify(Throwable error) {
        Throwable current = error;
        while (current != null) {
            if (current instanceof HttpStatusException) {
                return classifyHttpStatus(((HttpStatusException) current).statusCode);
            }
            if (current instanceof SyncContractException) {
                return Outcome.Status.PERMANENT_FAILURE;
            }
            if (current instanceof RetryableSyncException
                    || current instanceof IOException) {
                return Outcome.Status.RETRYABLE_FAILURE;
            }
            current = current.getCause();
        }
        return Outcome.Status.PERMANENT_FAILURE;
    }

    private static String compact(Throwable error) {
        if (error == null) {
            return "未知错误";
        }
        String message = clean(error.getMessage());
        String value = message.isEmpty()
                ? error.getClass().getSimpleName()
                : error.getClass().getSimpleName() + "：" + message;
        return value.length() > 240 ? value.substring(0, 240) : value;
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }

    @Override
    public void close() {
        store.close();
    }

    interface ProgressSink {
        ProgressSink NONE = stage -> {
        };

        void onStage(String stage);
    }

    static final class Outcome {
        enum Status {
            SUCCESS,
            BUSY,
            RETRYABLE_FAILURE,
            AUTH_REQUIRED,
            PERMANENT_FAILURE,
            NOT_CONFIGURED
        }

        final Status status;
        final String message;
        final int operationsUploaded;
        final int changesApplied;
        final int assetsDownloaded;
        final int assetsDeferred;
        final int booksFailed;
        final int assetTerminalFailures;
        final int metadataRepaired;
        final int metadataDeferred;
        final int metadataTerminalFailures;
        final boolean assetsRemain;
        final boolean dueAssetsRemain;
        final long nextAssetAttemptAtMillis;
        final boolean metadataRepairsRemain;
        final boolean dueMetadataRepairsRemain;
        final long nextMetadataRepairAtMillis;
        final boolean pendingOperationsRemain;

        Outcome(
                Status status,
                String message,
                int operationsUploaded,
                int changesApplied,
                int assetsDownloaded,
                int assetsDeferred,
                int booksFailed,
                int assetTerminalFailures,
                int metadataRepaired,
                int metadataDeferred,
                int metadataTerminalFailures,
                boolean assetsRemain,
                boolean dueAssetsRemain,
                long nextAssetAttemptAtMillis,
                boolean metadataRepairsRemain,
                boolean dueMetadataRepairsRemain,
                long nextMetadataRepairAtMillis,
                boolean pendingOperationsRemain
        ) {
            this.status = status;
            this.message = clean(message);
            this.operationsUploaded = Math.max(0, operationsUploaded);
            this.changesApplied = Math.max(0, changesApplied);
            this.assetsDownloaded = Math.max(0, assetsDownloaded);
            this.assetsDeferred = Math.max(0, assetsDeferred);
            this.booksFailed = Math.max(0, booksFailed);
            this.assetTerminalFailures = Math.max(0, assetTerminalFailures);
            this.metadataRepaired = Math.max(0, metadataRepaired);
            this.metadataDeferred = Math.max(0, metadataDeferred);
            this.metadataTerminalFailures = Math.max(0, metadataTerminalFailures);
            this.assetsRemain = assetsRemain;
            this.dueAssetsRemain = dueAssetsRemain;
            this.nextAssetAttemptAtMillis = nextAssetAttemptAtMillis;
            this.metadataRepairsRemain = metadataRepairsRemain;
            this.dueMetadataRepairsRemain = dueMetadataRepairsRemain;
            this.nextMetadataRepairAtMillis = nextMetadataRepairAtMillis;
            this.pendingOperationsRemain = pendingOperationsRemain;
        }

        static Outcome simple(Status status, String message) {
            return new Outcome(
                    status,
                    message,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    false,
                    false,
                    -1L,
                    false,
                    false,
                    -1L,
                    false
            );
        }
    }

    interface Transport {
        JSONObject requestJson(String method, String pathOrUrl, JSONObject body) throws Exception;

        void download(
                String pathOrUrl,
                File target,
                String expectedHash,
                long expectedBytes
        ) throws Exception;
    }

    private interface ExclusiveWork {
        Outcome run() throws Exception;
    }

    private interface Clock {
        long nowMillis();
    }

    private static final class MutableStats {
        int operationsUploaded;
        int changesApplied;
        int assetsDownloaded;
        int assetsDeferred;
        int booksFailed;
        int assetTerminalFailures;
        int metadataRepaired;
        int metadataDeferred;
        int metadataTerminalFailures;
    }

    private static final class UploadResult {
        static final UploadResult COMPLETE = new UploadResult(false);
        static final UploadResult RETRYABLE_REMAINS = new UploadResult(true);

        final boolean retryableOperationRemains;

        private UploadResult(boolean retryableOperationRemains) {
            this.retryableOperationRemains = retryableOperationRemains;
        }
    }

    private static final class AssetState {
        static final AssetState EMPTY = new AssetState(false, false, -1L);
        static final AssetState UNKNOWN = new AssetState(true, false, -1L);

        final boolean any;
        final boolean dueNow;
        final long nextAttemptAtMillis;

        AssetState(boolean any, boolean dueNow, long nextAttemptAtMillis) {
            this.any = any;
            this.dueNow = dueNow;
            this.nextAttemptAtMillis = nextAttemptAtMillis;
        }
    }

    private static final class MetadataRepairState {
        static final MetadataRepairState EMPTY =
                new MetadataRepairState(false, false, -1L);
        static final MetadataRepairState UNKNOWN =
                new MetadataRepairState(true, false, -1L);

        final boolean any;
        final boolean dueNow;
        final long nextAttemptAtMillis;

        MetadataRepairState(boolean any, boolean dueNow, long nextAttemptAtMillis) {
            this.any = any;
            this.dueNow = dueNow;
            this.nextAttemptAtMillis = nextAttemptAtMillis;
        }
    }

    static final class HttpStatusException extends IOException {
        final int statusCode;

        HttpStatusException(int statusCode, String responseText) {
            super("HTTP " + statusCode + " " + clean(responseText));
            this.statusCode = statusCode;
        }
    }

    static final class SyncContractException extends Exception {
        SyncContractException(String message) {
            super(message);
        }
    }

    static final class RetryableSyncException extends Exception {
        RetryableSyncException(String message) {
            super(message);
        }
    }

    private static final class HttpTransport implements Transport {
        private final ClickSyncConfig config;

        HttpTransport(ClickSyncConfig config) {
            this.config = config;
        }

        @Override
        public JSONObject requestJson(
                String method,
                String pathOrUrl,
                JSONObject body
        ) throws Exception {
            String text = requestText(method, pathOrUrl, body);
            if (clean(text).isEmpty()) {
                throw new SyncContractException("Reader API 返回空响应");
            }
            return new JSONObject(text);
        }

        private String requestText(
                String method,
                String pathOrUrl,
                JSONObject body
        ) throws Exception {
            String safeUrl = ReaderApiUrlPolicy.resolve(config.baseUrl, pathOrUrl);
            HttpURLConnection connection = (HttpURLConnection) new URL(safeUrl).openConnection();
            try {
                configure(connection, method);
                if (body != null) {
                    connection.setDoOutput(true);
                    connection.setRequestProperty(
                            "Content-Type",
                            "application/json; charset=utf-8"
                    );
                    byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
                    connection.setFixedLengthStreamingMode(bytes.length);
                    try (OutputStream output = connection.getOutputStream()) {
                        output.write(bytes);
                    }
                }
                int code = connection.getResponseCode();
                String response = readConnectionText(connection, code);
                if (code < 200 || code >= 300) {
                    throw new HttpStatusException(code, response);
                }
                return response;
            } finally {
                connection.disconnect();
            }
        }

        @Override
        public void download(
                String pathOrUrl,
                File target,
                String expectedHash,
                long expectedBytes
        ) throws Exception {
            VerifiedAssetFile.recover(target);
            if (target.isFile() && (!clean(expectedHash).isEmpty() || expectedBytes >= 0L)) {
                try {
                    VerifiedAssetFile.verify(target, expectedHash, expectedBytes);
                    return;
                } catch (Throwable ignored) {
                    // Keep the old complete file until a verified replacement is ready.
                }
            }

            File pending = VerifiedAssetFile.pending(target);
            if (pending.exists() && !pending.delete()) {
                throw new IOException("无法清理上次未完成的下载");
            }
            String safeUrl = ReaderApiUrlPolicy.resolve(config.baseUrl, pathOrUrl);
            HttpURLConnection connection = (HttpURLConnection) new URL(safeUrl).openConnection();
            try {
                configure(connection, "GET");
                int code = connection.getResponseCode();
                if (code < 200 || code >= 300) {
                    throw new HttpStatusException(code, readConnectionText(connection, code));
                }
                try (InputStream input = connection.getInputStream();
                     FileOutputStream output = new FileOutputStream(pending)) {
                    byte[] buffer = new byte[8192];
                    int read;
                    while ((read = input.read(buffer)) >= 0) {
                        if (Thread.currentThread().isInterrupted()) {
                            throw new InterruptedIOException("资源下载已停止");
                        }
                        output.write(buffer, 0, read);
                    }
                    output.getFD().sync();
                }
            } finally {
                connection.disconnect();
            }
            try {
                VerifiedAssetFile.verify(pending, expectedHash, expectedBytes);
                VerifiedAssetFile.replace(pending, target);
            } finally {
                if (pending.exists()) {
                    //noinspection ResultOfMethodCallIgnored
                    pending.delete();
                }
            }
        }

        private void configure(HttpURLConnection connection, String method)
                throws IOException {
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(8000);
            connection.setReadTimeout(180000);
            connection.setRequestMethod(method);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("X-Click-Device-Id", config.deviceId);
            connection.setRequestProperty("X-Click-Access-Token", config.accessToken);
            connection.setRequestProperty("Authorization", "Bearer " + config.accessToken);
        }

        private String readConnectionText(HttpURLConnection connection, int code)
                throws IOException {
            InputStream stream = code >= 200 && code < 300
                    ? connection.getInputStream()
                    : connection.getErrorStream();
            if (stream == null) {
                return "";
            }
            try (InputStream input = stream;
                 ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[8192];
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    output.write(buffer, 0, read);
                }
                return output.toString(StandardCharsets.UTF_8.name());
            }
        }
    }
}
