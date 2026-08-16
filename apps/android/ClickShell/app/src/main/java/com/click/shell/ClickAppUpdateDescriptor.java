package com.click.shell;

import org.json.JSONObject;

import java.util.Locale;
import java.util.regex.Pattern;

/** Strict, same-origin update manifest parsed before any APK bytes are accepted. */
final class ClickAppUpdateDescriptor {
    static final String SCHEMA = "click.android.app_update.v1";
    private static final Pattern HEX64 = Pattern.compile("^[0-9a-f]{64}$");
    private static final long MAX_APK_BYTES = 256L * 1024L * 1024L;

    final boolean available;
    final long versionCode;
    final String versionName;
    final String apkUrl;
    final long apkBytes;
    final String apkSha256;
    final String certificateSha256;
    final int minSdk;
    final boolean mandatory;
    final String releaseNotes;

    private ClickAppUpdateDescriptor(
            boolean available,
            long versionCode,
            String versionName,
            String apkUrl,
            long apkBytes,
            String apkSha256,
            String certificateSha256,
            int minSdk,
            boolean mandatory,
            String releaseNotes
    ) {
        this.available = available;
        this.versionCode = versionCode;
        this.versionName = versionName;
        this.apkUrl = apkUrl;
        this.apkBytes = apkBytes;
        this.apkSha256 = apkSha256;
        this.certificateSha256 = certificateSha256;
        this.minSdk = minSdk;
        this.mandatory = mandatory;
        this.releaseNotes = releaseNotes;
    }

    static ClickAppUpdateDescriptor parse(JSONObject payload, String baseUrl) {
        if (!payload.optBoolean("ok") || !SCHEMA.equals(payload.optString("schema"))) {
            throw new IllegalArgumentException("升级清单格式不正确");
        }
        if (!payload.optBoolean("available")) {
            return new ClickAppUpdateDescriptor(false, 0, "", "", 0, "", "", 28, false, "");
        }
        long versionCode = payload.optLong("version_code", 0);
        String versionName = clean(payload.optString("version_name"));
        long apkBytes = payload.optLong("apk_bytes", 0);
        String apkSha256 = clean(payload.optString("apk_sha256")).toLowerCase(Locale.ROOT);
        String certificateSha256 = clean(payload.optString("certificate_sha256")).toLowerCase(Locale.ROOT);
        int minSdk = payload.optInt("min_sdk", 28);
        String apkUrl = ReaderApiUrlPolicy.resolve(baseUrl, payload.optString("apk_url"));
        if (
                versionCode <= 0
                        || versionName.isEmpty()
                        || apkBytes <= 0
                        || apkBytes > MAX_APK_BYTES
                        || HEX64.matcher(apkSha256).matches() == false
                        || HEX64.matcher(certificateSha256).matches() == false
                        || minSdk < 28
        ) {
            throw new IllegalArgumentException("升级清单身份不完整");
        }
        return new ClickAppUpdateDescriptor(
                true,
                versionCode,
                versionName,
                apkUrl,
                apkBytes,
                apkSha256,
                certificateSha256,
                minSdk,
                payload.optBoolean("mandatory"),
                clean(payload.optString("release_notes"))
        );
    }

    boolean isNewerThan(long installedVersionCode) {
        return available && versionCode > installedVersionCode;
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }
}
