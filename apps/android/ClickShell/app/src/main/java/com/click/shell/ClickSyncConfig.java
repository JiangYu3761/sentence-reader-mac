package com.click.shell;

import android.content.Context;
import android.content.SharedPreferences;

import java.net.URI;
import java.util.Locale;
import java.util.UUID;

/** Runtime sync configuration loaded inside the app process; never put credentials in Work Data. */
final class ClickSyncConfig {
    static final String PREFERENCES_NAME = "ClickShellPrefs";
    static final String KEY_HOST = "host";
    static final String KEY_PORT = "port";
    static final String KEY_DEVICE_ID = "device_id";
    static final String KEY_ALLOW_METERED_ASSETS = "allow_metered_assets";
    static final String DEFAULT_PORT = "18180";
    static final boolean DEFAULT_ALLOW_METERED_ASSETS = false;

    final String baseUrl;
    final String deviceId;
    final String accessToken;
    final boolean allowMeteredAssets;

    private ClickSyncConfig(
            String baseUrl,
            String deviceId,
            String accessToken,
            boolean allowMeteredAssets
    ) {
        this.baseUrl = clean(baseUrl);
        this.deviceId = clean(deviceId);
        this.accessToken = clean(accessToken);
        this.allowMeteredAssets = allowMeteredAssets;
    }

    static ClickSyncConfig load(Context context) {
        return load(context, false);
    }

    static ClickSyncConfig loadReadOnly(Context context) {
        return load(context, true);
    }

    private static ClickSyncConfig load(Context context, boolean readOnly) {
        Context appContext = context.getApplicationContext();
        SharedPreferences preferences = appContext.getSharedPreferences(
                PREFERENCES_NAME,
                Context.MODE_PRIVATE
        );
        String baseUrl = normalizeBaseUrl(
                preferences.getString(KEY_HOST, ""),
                preferences.getString(KEY_PORT, DEFAULT_PORT)
        );
        String accessToken = "";
        try {
            AndroidCredentialStore credentialStore = new AndroidCredentialStore(preferences);
            accessToken = readOnly
                    ? credentialStore.accessTokenReadOnly()
                    : credentialStore.accessToken();
        } catch (Throwable ignored) {
            // A missing or unavailable Keystore entry is a terminal not-configured state, not a retry loop.
        }
        return new ClickSyncConfig(
                baseUrl,
                preferences.getString(KEY_DEVICE_ID, ""),
                accessToken,
                preferences.getBoolean(
                        KEY_ALLOW_METERED_ASSETS,
                        DEFAULT_ALLOW_METERED_ASSETS
                )
        );
    }

    static boolean allowMeteredAssets(Context context) {
        Context appContext = context.getApplicationContext();
        return appContext.getSharedPreferences(PREFERENCES_NAME, Context.MODE_PRIVATE)
                .getBoolean(
                        KEY_ALLOW_METERED_ASSETS,
                        DEFAULT_ALLOW_METERED_ASSETS
                );
    }

    static boolean setAllowMeteredAssets(Context context, boolean allowMeteredAssets) {
        Context appContext = context.getApplicationContext();
        return appContext.getSharedPreferences(PREFERENCES_NAME, Context.MODE_PRIVATE)
                .edit()
                .putBoolean(KEY_ALLOW_METERED_ASSETS, allowMeteredAssets)
                .commit();
    }

    static String ensureDeviceId(Context context) {
        Context appContext = context.getApplicationContext();
        SharedPreferences preferences = appContext.getSharedPreferences(
                PREFERENCES_NAME,
                Context.MODE_PRIVATE
        );
        String existing = clean(preferences.getString(KEY_DEVICE_ID, ""));
        if (!existing.isEmpty()) {
            return existing;
        }
        String created = "android-" + UUID.randomUUID();
        preferences.edit().putString(KEY_DEVICE_ID, created).apply();
        return created;
    }

    boolean isReady() {
        return !baseUrl.isEmpty() && !deviceId.isEmpty() && !accessToken.isEmpty();
    }

    static String normalizeBaseUrl(String hostInput, String portInput) {
        String hostValue = clean(hostInput);
        if (hostValue.isEmpty()) {
            return "";
        }
        try {
            boolean explicitOrigin = hostValue.contains("://");
            String raw = explicitOrigin ? hostValue : "http://" + hostValue;
            URI parsed = new URI(raw);
            String host = clean(parsed.getHost());
            String scheme = clean(parsed.getScheme()).toLowerCase(Locale.ROOT);
            if (host.isEmpty() || (!"http".equals(scheme) && !"https".equals(scheme))) {
                return "";
            }
            int port = parsed.getPort();
            if (port <= 0) {
                if (explicitOrigin) {
                    port = "https".equals(scheme) ? 443 : 80;
                } else {
                    port = normalizePort(portInput);
                }
            }
            return new URI(scheme, null, host, port, null, null, null).toASCIIString();
        } catch (Throwable ignored) {
            return "";
        }
    }

    private static int normalizePort(String value) {
        try {
            int port = Integer.parseInt(clean(value));
            return port > 0 && port <= 65535 ? port : Integer.parseInt(DEFAULT_PORT);
        } catch (Throwable ignored) {
            return Integer.parseInt(DEFAULT_PORT);
        }
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }
}
