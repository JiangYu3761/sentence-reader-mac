package com.click.shell;

import android.content.ContentResolver;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.OpenableColumns;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.security.MessageDigest;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.zip.ZipEntry;
import java.util.zip.ZipFile;

/**
 * Copies one user-selected EPUB/PDF into Click's permanent private storage before registration.
 *
 * <p>The content URI is never retained as the book source. A complete, validated original is
 * installed atomically first; the local book row and durable Mac-import queue are then committed
 * by {@link ClickNativeStore}. Network availability is not part of this operation.</p>
 */
final class BookImportIngress {
    static final long MAX_SOURCE_BYTES = 120L * 1024L * 1024L;
    static final String SOURCE_ROOT_NAME = "book-import-originals";
    static final String LOCAL_ID_PREFIX = "android-local-";

    private static final int BUFFER_BYTES = 64 * 1024;
    private static final Object IMPORT_LOCK = new Object();

    private final Context appContext;
    private final ContentResolver resolver;

    BookImportIngress(Context context) {
        appContext = context.getApplicationContext();
        resolver = appContext.getContentResolver();
    }

    Result importContent(Uri uri) throws ImportException {
        synchronized (IMPORT_LOCK) {
            return importContentLocked(uri);
        }
    }

    private Result importContentLocked(Uri uri) throws ImportException {
        if (uri == null || !"content".equalsIgnoreCase(uri.getScheme())) {
            throw new ImportException("请从 Android 文件选择器选择 EPUB 或 PDF 文件");
        }

        File root = new File(appContext.getFilesDir(), SOURCE_ROOT_NAME);
        ensureDirectory(root);
        File incoming = new File(
                root,
                ".incoming-" + UUID.randomUUID() + ".part"
        );
        String displayName = displayName(uri);
        CopiedSource copied;
        try (InputStream input = resolver.openInputStream(uri)) {
            if (input == null) {
                throw new ImportException("Android 无法打开这个文件，请重新选择");
            }
            copied = copyAndHash(input, incoming, MAX_SOURCE_BYTES);
        } catch (ImportException error) {
            deleteQuietly(incoming);
            throw error;
        } catch (SecurityException error) {
            deleteQuietly(incoming);
            throw new ImportException("Android 没有读取这个文件的权限，请重新选择", error);
        } catch (IOException error) {
            deleteQuietly(incoming);
            throw new ImportException("读取文件失败，请确认文件仍然存在且存储空间可用", error);
        }

        File targetPart = null;
        try {
            String sourceKind = detectSourceKind(incoming);
            String localBookId = stableLocalBookId(copied.sha256);
            String filename = safeFilename(displayName, sourceKind);
            String title = titleFromFilename(filename, sourceKind);

            File bookDirectory = new File(root, copied.sha256);
            ensureDirectory(bookDirectory);
            File target = new File(bookDirectory, "original." + sourceKind);
            targetPart = VerifiedAssetFile.pending(target);
            boolean alreadyStored = installOriginal(
                    incoming,
                    targetPart,
                    target,
                    copied,
                    sourceKind
            );
            List<EpubOfflineIndexer.Chapter> offlineChapters = "epub".equals(sourceKind)
                    ? EpubOfflineIndexer.index(target)
                    : Collections.emptyList();

            try (ClickNativeStore store = new ClickNativeStore(appContext)) {
                store.registerLocalBookImport(
                        localBookId,
                        title,
                        "",
                        sourceKind,
                        target.getAbsolutePath(),
                        copied.sha256,
                        copied.byteSize,
                        filename,
                        offlineChapters
                );
            } catch (Throwable error) {
                // Keep the verified original. Selecting the same file again reuses it safely.
                throw new ImportException(
                        "原书已经安全保存在手机，但书库登记失败，请稍后重新选择一次",
                        error
                );
            }

            boolean uploadWakeScheduled = true;
            try {
                BookImportScheduler.enqueue(appContext);
            } catch (Throwable ignored) {
                // The database queue is authoritative. A later network/app event can wake it.
                uploadWakeScheduled = false;
            }
            return new Result(
                    localBookId,
                    title,
                    sourceKind,
                    copied.sha256,
                    copied.byteSize,
                    alreadyStored,
                    uploadWakeScheduled
            );
        } catch (ImportException error) {
            throw error;
        } catch (Throwable error) {
            throw new ImportException(
                    "无法安全保存这本书，请确认它是有效的 EPUB 或 PDF",
                    error
            );
        } finally {
            deleteQuietly(incoming);
            if (targetPart != null) {
                deleteQuietly(targetPart);
            }
        }
    }

    private boolean installOriginal(
            File incoming,
            File targetPart,
            File target,
            CopiedSource copied,
            String sourceKind
    ) throws Exception {
        if (target.isFile()) {
            try {
                VerifiedAssetFile.verify(target, copied.sha256, copied.byteSize);
                if (sourceKind.equals(detectSourceKind(target))) {
                    return true;
                }
            } catch (Throwable ignored) {
                // The verified incoming copy replaces only this corrupt content-addressed target.
            }
        }
        if (targetPart.exists() && !targetPart.delete()) {
            throw new ImportException("上次导入未能清理，请稍后重试");
        }
        if (!incoming.renameTo(targetPart)) {
            throw new ImportException("无法把原书移入 Click 私有目录，请确认手机存储空间");
        }
        VerifiedAssetFile.replace(targetPart, target);
        if (!target.isFile() || target.length() != copied.byteSize) {
            throw new ImportException("原书保存后校验失败，请重新导入");
        }
        return false;
    }

    private String displayName(Uri uri) {
        try (Cursor cursor = resolver.query(
                uri,
                new String[]{OpenableColumns.DISPLAY_NAME},
                null,
                null,
                null
        )) {
            if (cursor != null && cursor.moveToFirst() && !cursor.isNull(0)) {
                return cursor.getString(0);
            }
        } catch (Throwable ignored) {
            // The provider name is optional; content validation remains authoritative.
        }
        String fallback = uri.getLastPathSegment();
        return fallback == null ? "" : fallback;
    }

    static CopiedSource copyAndHash(
            InputStream input,
            File target,
            long maxBytes
    ) throws ImportException {
        if (input == null || target == null || maxBytes <= 0L) {
            throw new ImportException("无法读取所选文件");
        }
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            long total = 0L;
            try (FileOutputStream output = new FileOutputStream(target, false)) {
                byte[] buffer = new byte[BUFFER_BYTES];
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    if (Thread.currentThread().isInterrupted()) {
                        throw new ImportException("导入已停止，原书没有被登记");
                    }
                    if (read == 0) {
                        int single = input.read();
                        if (single < 0) {
                            break;
                        }
                        buffer[0] = (byte) single;
                        read = 1;
                    }
                    if (total > maxBytes - read) {
                        throw new ImportException("文件超过 120 MB，本轮暂不支持导入");
                    }
                    digest.update(buffer, 0, read);
                    output.write(buffer, 0, read);
                    total += read;
                }
                output.flush();
                output.getFD().sync();
            }
            if (total <= 0L) {
                throw new ImportException("这个文件是空的，无法导入");
            }
            return new CopiedSource(hex(digest.digest()), total);
        } catch (ImportException error) {
            deleteQuietly(target);
            throw error;
        } catch (Throwable error) {
            deleteQuietly(target);
            throw new ImportException("复制原书失败，请确认手机存储空间可用", error);
        }
    }

    static String detectSourceKind(File file) throws ImportException {
        byte[] magic = new byte[5];
        int magicBytes;
        try (InputStream input = new FileInputStream(file)) {
            magicBytes = input.read(magic);
        } catch (IOException error) {
            throw new ImportException("无法校验所选文件", error);
        }
        if (magicBytes >= 5
                && magic[0] == '%'
                && magic[1] == 'P'
                && magic[2] == 'D'
                && magic[3] == 'F'
                && magic[4] == '-') {
            return "pdf";
        }
        if (magicBytes >= 4
                && magic[0] == 'P'
                && magic[1] == 'K'
                && magic[2] == 3
                && magic[3] == 4) {
            validateEpub(file);
            return "epub";
        }
        throw new ImportException("这不是有效的 EPUB 或 PDF 文件");
    }

    private static void validateEpub(File file) throws ImportException {
        try (ZipFile archive = new ZipFile(file)) {
            if (archive.size() == 0) {
                throw new ImportException("EPUB 压缩包是空的");
            }
            ZipEntry mime = archive.getEntry("mimetype");
            ZipEntry container = archive.getEntry("META-INF/container.xml");
            if (mime == null
                    || mime.isDirectory()
                    || container == null
                    || container.isDirectory()) {
                throw new ImportException("EPUB 结构不完整，缺少标准书籍目录");
            }
            String mimeValue;
            try (InputStream input = archive.getInputStream(mime)) {
                mimeValue = readSmallUtf8(input, 64).trim();
            }
            if (!"application/epub+zip".equals(mimeValue)) {
                throw new ImportException("EPUB 的格式标记不正确");
            }
        } catch (ImportException error) {
            throw error;
        } catch (Throwable error) {
            throw new ImportException("EPUB 压缩包已损坏或无法读取", error);
        }
    }

    private static String readSmallUtf8(InputStream input, int limit) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[32];
        int read;
        while ((read = input.read(buffer)) >= 0) {
            if (read == 0) {
                int single = input.read();
                if (single < 0) {
                    break;
                }
                buffer[0] = (byte) single;
                read = 1;
            }
            if (output.size() > limit - read) {
                throw new IOException("entry is too large");
            }
            output.write(buffer, 0, read);
        }
        return output.toString("UTF-8");
    }

    static String stableLocalBookId(String sha256) throws ImportException {
        String normalized = sha256 == null
                ? ""
                : sha256.trim().toLowerCase(Locale.ROOT);
        if (!normalized.matches("[0-9a-f]{64}")) {
            throw new ImportException("无法为原书生成稳定身份");
        }
        return LOCAL_ID_PREFIX + normalized;
    }

    static String safeFilename(String rawName, String sourceKind) {
        String kind = "pdf".equals(sourceKind) ? "pdf" : "epub";
        String value = rawName == null ? "" : rawName.trim();
        int slash = Math.max(value.lastIndexOf('/'), value.lastIndexOf('\\'));
        if (slash >= 0 && slash + 1 < value.length()) {
            value = value.substring(slash + 1);
        }
        String lower = value.toLowerCase(Locale.ROOT);
        if (lower.endsWith(".epub")) {
            value = value.substring(0, value.length() - 5);
        } else if (lower.endsWith(".pdf")) {
            value = value.substring(0, value.length() - 4);
        }
        value = value
                .replaceAll("[\\p{Cntrl}/\\\\]+", " ")
                .replaceAll("\\s+", " ")
                .trim();
        while (value.startsWith(".")) {
            value = value.substring(1).trim();
        }
        if (value.isEmpty()) {
            value = "未命名书籍";
        }
        if (value.length() > 100) {
            value = value.substring(0, 100).trim();
        }
        return value + "." + kind;
    }

    static String titleFromFilename(String filename, String sourceKind) {
        String suffix = "." + ("pdf".equals(sourceKind) ? "pdf" : "epub");
        String value = filename == null ? "" : filename.trim();
        if (value.toLowerCase(Locale.ROOT).endsWith(suffix)) {
            value = value.substring(0, value.length() - suffix.length()).trim();
        }
        return value.isEmpty() ? "未命名书籍" : value;
    }

    private static String hex(byte[] value) {
        StringBuilder result = new StringBuilder(value.length * 2);
        for (byte item : value) {
            result.append(String.format(Locale.ROOT, "%02x", item & 0xff));
        }
        return result.toString();
    }

    private static void ensureDirectory(File directory) throws ImportException {
        if ((!directory.isDirectory() && !directory.mkdirs()) || !directory.isDirectory()) {
            throw new ImportException("无法创建 Click 私有书库，请确认手机存储空间");
        }
    }

    private static void deleteQuietly(File file) {
        if (file != null && file.exists()) {
            file.delete();
        }
    }

    static final class CopiedSource {
        final String sha256;
        final long byteSize;

        CopiedSource(String sha256, long byteSize) {
            this.sha256 = sha256;
            this.byteSize = byteSize;
        }
    }

    static final class Result {
        final String localBookId;
        final String title;
        final String sourceKind;
        final String sourceHash;
        final long sourceByteSize;
        final boolean alreadyStored;
        final boolean uploadWakeScheduled;

        Result(
                String localBookId,
                String title,
                String sourceKind,
                String sourceHash,
                long sourceByteSize,
                boolean alreadyStored,
                boolean uploadWakeScheduled
        ) {
            this.localBookId = localBookId;
            this.title = title;
            this.sourceKind = sourceKind;
            this.sourceHash = sourceHash;
            this.sourceByteSize = sourceByteSize;
            this.alreadyStored = alreadyStored;
            this.uploadWakeScheduled = uploadWakeScheduled;
        }
    }

    static final class ImportException extends Exception {
        ImportException(String message) {
            super(message);
        }

        ImportException(String message, Throwable cause) {
            super(message, cause);
        }
    }
}
