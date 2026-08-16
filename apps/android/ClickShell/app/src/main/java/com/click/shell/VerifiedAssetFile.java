package com.click.shell;

import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.security.MessageDigest;

final class VerifiedAssetFile {
    private VerifiedAssetFile() {
    }

    static File pending(File target) {
        return new File(target.getParentFile(), target.getName() + ".part");
    }

    static File backup(File target) {
        return new File(target.getParentFile(), target.getName() + ".previous");
    }

    static void recover(File target) throws IOException {
        File backup = backup(target);
        if (target.isFile()) {
            if (backup.exists() && !backup.delete()) {
                throw new IOException("cannot remove stale asset backup");
            }
            return;
        }
        if (backup.isFile() && !backup.renameTo(target)) {
            throw new IOException("cannot restore previous asset");
        }
    }

    static void verify(File candidate, String expectedSha256, long expectedBytes) throws Exception {
        if (!candidate.isFile() || candidate.length() <= 0L) {
            throw new IllegalStateException("downloaded asset is empty");
        }
        if (expectedBytes >= 0L && candidate.length() != expectedBytes) {
            throw new IllegalStateException(
                    "downloaded asset size mismatch: " + candidate.length() + " != " + expectedBytes
            );
        }
        String normalizedHash = expectedSha256 == null ? "" : expectedSha256.trim().toLowerCase();
        if (!normalizedHash.isEmpty() && !normalizedHash.equals(sha256(candidate))) {
            throw new IllegalStateException("downloaded asset hash mismatch");
        }
    }

    static void replace(File pending, File target) throws IOException {
        if (!pending.isFile()) {
            throw new IOException("verified pending asset is missing");
        }
        recover(target);
        File backup = backup(target);
        boolean hadTarget = target.isFile();
        if (hadTarget && !target.renameTo(backup)) {
            throw new IOException("cannot preserve previous asset");
        }
        if (!pending.renameTo(target)) {
            if (hadTarget && backup.isFile() && !backup.renameTo(target)) {
                throw new IOException("cannot install or restore asset");
            }
            throw new IOException("cannot install verified asset");
        }
        if (backup.exists() && !backup.delete()) {
            backup.deleteOnExit();
        }
    }

    static String sha256(File file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (FileInputStream input = new FileInputStream(file)) {
            byte[] buffer = new byte[8192];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                if (read > 0) {
                    digest.update(buffer, 0, read);
                }
            }
        }
        StringBuilder value = new StringBuilder();
        for (byte item : digest.digest()) {
            value.append(String.format("%02x", item & 0xff));
        }
        return value.toString();
    }
}
