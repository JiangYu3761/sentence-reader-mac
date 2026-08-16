package com.click.shell;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.net.Uri;
import android.os.Build;
import android.provider.Settings;
import android.widget.Toast;

import androidx.core.content.FileProvider;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;

/** Foreground-triggered, non-polling updater for private signed Click APKs. */
final class ClickAppUpdater {
    interface Listener {
        void onResult(String state, String detail, File apk);
    }

    private static final String PREFS = "ClickAppUpdatePrefs";
    private static final String KEY_NEXT_CHECK_AFTER = "next_check_after_ms";
    private static final long SUCCESS_INTERVAL_MS = 24L * 60L * 60L * 1000L;
    private static final long FAILURE_INTERVAL_MS = 6L * 60L * 60L * 1000L;
    private static final int MAX_MANIFEST_BYTES = 64 * 1024;
    private static final AtomicBoolean CHECK_RUNNING = new AtomicBoolean(false);
    private static volatile File pendingInstall;
    private static volatile boolean waitingForUnknownSources;
    private static volatile boolean installerLaunched;

    private ClickAppUpdater() {
    }

    static void checkOnForeground(Activity activity) {
        ClickSyncConfig config = ClickSyncConfig.loadReadOnly(activity);
        if (!config.isReady()) {
            return;
        }
        SharedPreferences preferences = activity.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        long now = System.currentTimeMillis();
        if (now < preferences.getLong(KEY_NEXT_CHECK_AFTER, 0L)) {
            return;
        }
        check(activity, config.baseUrl, config.deviceId, config.accessToken, false, true, null);
    }

    static void resumePendingInstall(Activity activity) {
        if (!waitingForUnknownSources || pendingInstall == null || installerLaunched) {
            return;
        }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O
                || activity.getPackageManager().canRequestPackageInstalls()) {
            waitingForUnknownSources = false;
            launchPackageInstaller(activity, pendingInstall);
        }
    }

    static void checkForTest(
            Activity activity,
            String baseUrl,
            String deviceId,
            String accessToken,
            boolean launchInstaller,
            Listener listener
    ) {
        check(activity, baseUrl, deviceId, accessToken, true, launchInstaller, listener);
    }

    private static void check(
            Activity activity,
            String baseUrl,
            String deviceId,
            String accessToken,
            boolean force,
            boolean launchInstaller,
            Listener listener
    ) {
        if (!CHECK_RUNNING.compareAndSet(false, true)) {
            notifyListener(activity, listener, "busy", "升级检查已经在运行", null);
            return;
        }
        Context appContext = activity.getApplicationContext();
        Thread worker = new Thread(() -> {
            android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_BACKGROUND);
            long nextCheck = System.currentTimeMillis() + FAILURE_INTERVAL_MS;
            try {
                ClickAppUpdateDescriptor descriptor = requestManifest(
                        baseUrl,
                        deviceId,
                        accessToken
                );
                nextCheck = System.currentTimeMillis() + SUCCESS_INTERVAL_MS;
                if (!descriptor.isNewerThan(BuildConfig.VERSION_CODE)) {
                    notifyListener(activity, listener, "current", "已经是最新版本", null);
                    return;
                }
                if (descriptor.minSdk > Build.VERSION.SDK_INT) {
                    throw new IllegalStateException("新版需要更高的 Android 系统版本");
                }
                File verified = obtainVerifiedApk(
                        appContext,
                        descriptor,
                        deviceId,
                        accessToken
                );
                // A previous system-installer visit may have been cancelled while this app
                // process stayed alive. A later foreground check must be allowed to present
                // the newly verified candidate again.
                installerLaunched = false;
                pendingInstall = verified;
                notifyListener(activity, listener, "downloaded", descriptor.versionName, verified);
                if (launchInstaller) {
                    activity.runOnUiThread(() -> beginInstallFlow(activity, verified));
                }
            } catch (Throwable error) {
                notifyListener(activity, listener, "error", compact(error), null);
                if (force) {
                    activity.runOnUiThread(() -> Toast.makeText(
                            activity,
                            "检查更新失败：" + compact(error),
                            Toast.LENGTH_LONG
                    ).show());
                }
            } finally {
                appContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                        .edit()
                        .putLong(KEY_NEXT_CHECK_AFTER, nextCheck)
                        .apply();
                CHECK_RUNNING.set(false);
            }
        }, "click-app-update");
        worker.setDaemon(true);
        worker.start();
    }

    private static ClickAppUpdateDescriptor requestManifest(
            String baseUrl,
            String deviceId,
            String accessToken
    ) throws Exception {
        String endpoint = ReaderApiUrlPolicy.resolve(
                baseUrl,
                "/v1/android/app-update?current_version_code=" + BuildConfig.VERSION_CODE
        );
        HttpURLConnection connection = authorizedConnection(endpoint, deviceId, accessToken);
        connection.setConnectTimeout(8_000);
        connection.setReadTimeout(15_000);
        try {
            int code = connection.getResponseCode();
            if (code < 200 || code >= 300) {
                throw new IllegalStateException("升级服务 HTTP " + code);
            }
            byte[] body = readBounded(connection.getInputStream(), MAX_MANIFEST_BYTES);
            return ClickAppUpdateDescriptor.parse(
                    new JSONObject(new String(body, StandardCharsets.UTF_8)),
                    baseUrl
            );
        } finally {
            connection.disconnect();
        }
    }

    private static File obtainVerifiedApk(
            Context context,
            ClickAppUpdateDescriptor descriptor,
            String deviceId,
            String accessToken
    ) throws Exception {
        File root = new File(context.getFilesDir(), "app-updates");
        if (!root.isDirectory() && !root.mkdirs()) {
            throw new IllegalStateException("无法创建升级目录");
        }
        String filename = "Click-" + descriptor.versionCode + "-" + descriptor.apkSha256.substring(0, 12) + ".apk";
        File target = new File(root, filename);
        if (target.isFile()
                && target.length() == descriptor.apkBytes
                && descriptor.apkSha256.equals(VerifiedAssetFile.sha256(target))) {
            verifyPackageIdentity(context, target, descriptor);
            deleteOtherUpdates(root, target);
            return target;
        }
        target.delete();
        File pending = new File(root, filename + ".partial");
        pending.delete();
        HttpURLConnection connection = authorizedConnection(descriptor.apkUrl, deviceId, accessToken);
        connection.setConnectTimeout(10_000);
        connection.setReadTimeout(120_000);
        try {
            int code = connection.getResponseCode();
            if (code < 200 || code >= 300) {
                throw new IllegalStateException("下载升级包 HTTP " + code);
            }
            long declaredLength = connection.getContentLengthLong();
            if (declaredLength > 0 && declaredLength != descriptor.apkBytes) {
                throw new IllegalStateException("升级包长度与清单不一致");
            }
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            long written = 0L;
            try (
                    InputStream input = connection.getInputStream();
                    FileOutputStream output = new FileOutputStream(pending)
            ) {
                byte[] buffer = new byte[64 * 1024];
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    if (read == 0) {
                        continue;
                    }
                    written += read;
                    if (written > descriptor.apkBytes) {
                        throw new IllegalStateException("升级包超过清单长度");
                    }
                    output.write(buffer, 0, read);
                    digest.update(buffer, 0, read);
                }
                output.getFD().sync();
            }
            if (written != descriptor.apkBytes
                    || !descriptor.apkSha256.equals(hex(digest.digest()))) {
                throw new IllegalStateException("升级包完整性校验失败");
            }
            if (!pending.renameTo(target)) {
                throw new IllegalStateException("升级包原子提交失败");
            }
            verifyPackageIdentity(context, target, descriptor);
            deleteOtherUpdates(root, target);
            return target;
        } catch (Throwable error) {
            pending.delete();
            target.delete();
            throw error;
        } finally {
            connection.disconnect();
        }
    }

    private static void verifyPackageIdentity(
            Context context,
            File apk,
            ClickAppUpdateDescriptor descriptor
    ) throws Exception {
        PackageManager manager = context.getPackageManager();
        PackageInfo current = manager.getPackageInfo(
                context.getPackageName(),
                PackageManager.GET_SIGNING_CERTIFICATES
        );
        PackageInfo candidate = manager.getPackageArchiveInfo(
                apk.getAbsolutePath(),
                PackageManager.GET_SIGNING_CERTIFICATES
        );
        if (candidate == null
                || !context.getPackageName().equals(candidate.packageName)
                || candidate.getLongVersionCode() != descriptor.versionCode
                || candidate.getLongVersionCode() <= current.getLongVersionCode()) {
            throw new SecurityException("升级包应用身份或版本不正确");
        }
        String currentSigner = singleSignerSha256(current);
        String candidateSigner = singleSignerSha256(candidate);
        if (!currentSigner.equals(candidateSigner)
                || !candidateSigner.equals(descriptor.certificateSha256)) {
            throw new SecurityException("升级包签名与当前 Click 不一致");
        }
    }

    private static String singleSignerSha256(PackageInfo info) throws Exception {
        if (info.signingInfo == null) {
            throw new SecurityException("APK 缺少签名信息");
        }
        Signature[] signatures = info.signingInfo.getApkContentsSigners();
        if (signatures == null || signatures.length != 1) {
            throw new SecurityException("APK 签名数量不受支持");
        }
        return hex(MessageDigest.getInstance("SHA-256").digest(signatures[0].toByteArray()));
    }

    private static void beginInstallFlow(Activity activity, File apk) {
        if (installerLaunched || !apk.isFile()) {
            return;
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                && !activity.getPackageManager().canRequestPackageInstalls()) {
            waitingForUnknownSources = true;
            Intent settings = new Intent(
                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + activity.getPackageName())
            );
            activity.startActivity(settings);
            return;
        }
        launchPackageInstaller(activity, apk);
    }

    private static void launchPackageInstaller(Activity activity, File apk) {
        installerLaunched = true;
        pendingInstall = apk;
        Uri uri = FileProvider.getUriForFile(
                activity,
                activity.getPackageName() + ".bookfiles",
                apk
        );
        Intent install = new Intent(Intent.ACTION_VIEW)
                .setDataAndType(uri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        Toast.makeText(activity, "新版已验证，正在打开系统安装界面", Toast.LENGTH_SHORT).show();
        activity.startActivity(install);
    }

    private static HttpURLConnection authorizedConnection(
            String url,
            String deviceId,
            String accessToken
    ) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(url).openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setRequestMethod("GET");
        connection.setRequestProperty("Accept", "application/json, application/vnd.android.package-archive");
        connection.setRequestProperty("X-Click-Device-Id", clean(deviceId));
        String token = clean(accessToken);
        if (!token.isEmpty()) {
            connection.setRequestProperty("X-Click-Access-Token", token);
            connection.setRequestProperty("Authorization", "Bearer " + token);
        }
        return connection;
    }

    private static byte[] readBounded(InputStream input, int maximum) throws Exception {
        try (InputStream source = input; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[4096];
            int total = 0;
            int read;
            while ((read = source.read(buffer)) >= 0) {
                total += read;
                if (total > maximum) {
                    throw new IllegalStateException("升级清单过大");
                }
                output.write(buffer, 0, read);
            }
            return output.toByteArray();
        }
    }

    private static void deleteOtherUpdates(File root, File keep) {
        File[] files = root.listFiles();
        if (files == null) {
            return;
        }
        for (File file : files) {
            if (file.isFile() && !file.equals(keep)) {
                file.delete();
            }
        }
    }

    private static void notifyListener(
            Activity activity,
            Listener listener,
            String state,
            String detail,
            File apk
    ) {
        if (listener != null) {
            activity.runOnUiThread(() -> listener.onResult(state, detail, apk));
        }
    }

    private static String hex(byte[] bytes) {
        StringBuilder value = new StringBuilder(bytes.length * 2);
        for (byte item : bytes) {
            value.append(String.format(Locale.ROOT, "%02x", item & 0xff));
        }
        return value.toString();
    }

    private static String compact(Throwable error) {
        String value = error.getMessage();
        if (value == null || value.trim().isEmpty()) {
            value = error.getClass().getSimpleName();
        }
        return value.length() > 140 ? value.substring(0, 140) : value;
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }
}
