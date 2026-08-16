package com.click.shell;

import android.content.Context;
import android.content.SharedPreferences;
import android.net.nsd.NsdManager;
import android.net.nsd.NsdServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.InetAddress;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.Map;
import java.util.concurrent.Executor;

/**
 * Performs a short, foreground-only Bonjour discovery after launch or a Wi-Fi change.
 *
 * <p>There is no polling and no background service. A candidate is persisted only after the
 * already-paired device token authenticates against Click's protected Android health route.</p>
 */
final class ClickLanDiscovery {
    interface Listener {
        void onVerifiedOrigin(String origin);
    }

    static final String SERVICE_TYPE = "_click-reader._tcp.";
    static final String SERVICE_CONTRACT = "click.reader_runtime.v1";
    static final long DISCOVERY_WINDOW_MILLIS = 8_000L;

    private final Context appContext;
    private final Executor serialExecutor;
    private final Listener listener;
    private final NsdManager nsdManager;
    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ArrayDeque<NsdServiceInfo> pendingServices = new ArrayDeque<>();
    private final Runnable discoveryTimeout = this::stopCurrentDiscovery;

    private NsdManager.DiscoveryListener discoveryListener;
    private boolean started;
    private boolean resolving;
    private int generation;

    ClickLanDiscovery(Context context, Executor serialExecutor, Listener listener) {
        this.appContext = context.getApplicationContext();
        this.serialExecutor = serialExecutor;
        this.listener = listener;
        this.nsdManager = appContext.getSystemService(NsdManager.class);
    }

    void start() {
        if (started) {
            return;
        }
        started = true;
        refresh();
    }

    void stop() {
        if (!started && discoveryListener == null) {
            return;
        }
        started = false;
        generation += 1;
        stopCurrentDiscovery();
        pendingServices.clear();
        resolving = false;
    }

    void refresh() {
        if (!started || nsdManager == null) {
            return;
        }
        ClickSyncConfig config = ClickSyncConfig.load(appContext);
        if (!config.isReady()) {
            stopCurrentDiscovery();
            return;
        }
        stopCurrentDiscovery();
        pendingServices.clear();
        resolving = false;
        int currentGeneration = ++generation;
        NsdManager.DiscoveryListener created = new NsdManager.DiscoveryListener() {
            @Override
            public void onDiscoveryStarted(String serviceType) {
                // Bounded by discoveryTimeout; no periodic restart.
            }

            @Override
            public void onServiceFound(NsdServiceInfo serviceInfo) {
                if (!isCurrent(currentGeneration)
                        || !serviceTypeMatches(serviceInfo.getServiceType())) {
                    return;
                }
                pendingServices.addLast(serviceInfo);
                resolveNext(currentGeneration, config);
            }

            @Override
            public void onServiceLost(NsdServiceInfo serviceInfo) {
                // The next foreground/network recovery event performs a fresh discovery.
            }

            @Override
            public void onDiscoveryStopped(String serviceType) {
                if (discoveryListener == this) {
                    discoveryListener = null;
                }
            }

            @Override
            public void onStartDiscoveryFailed(String serviceType, int errorCode) {
                if (discoveryListener == this) {
                    stopCurrentDiscovery();
                }
            }

            @Override
            public void onStopDiscoveryFailed(String serviceType, int errorCode) {
                if (discoveryListener == this) {
                    discoveryListener = null;
                }
            }
        };
        discoveryListener = created;
        try {
            nsdManager.discoverServices(
                    SERVICE_TYPE,
                    NsdManager.PROTOCOL_DNS_SD,
                    created
            );
            mainHandler.removeCallbacks(discoveryTimeout);
            mainHandler.postDelayed(discoveryTimeout, DISCOVERY_WINDOW_MILLIS);
        } catch (RuntimeException ignored) {
            discoveryListener = null;
        }
    }

    private void resolveNext(int currentGeneration, ClickSyncConfig config) {
        if (!isCurrent(currentGeneration) || resolving) {
            return;
        }
        NsdServiceInfo service = pendingServices.pollFirst();
        if (service == null) {
            return;
        }
        resolving = true;
        try {
            nsdManager.resolveService(service, new NsdManager.ResolveListener() {
                @Override
                public void onResolveFailed(NsdServiceInfo serviceInfo, int errorCode) {
                    resolving = false;
                    resolveNext(currentGeneration, config);
                }

                @Override
                public void onServiceResolved(NsdServiceInfo serviceInfo) {
                    if (!isCurrent(currentGeneration)
                            || !hasExpectedContract(serviceInfo)) {
                        resolving = false;
                        resolveNext(currentGeneration, config);
                        return;
                    }
                    String origin = resolvedPrivateOrigin(serviceInfo);
                    if (origin.isEmpty()) {
                        resolving = false;
                        resolveNext(currentGeneration, config);
                        return;
                    }
                    try {
                        serialExecutor.execute(
                                () -> verifyAndAccept(
                                        currentGeneration,
                                        origin,
                                        serviceInfo.getPort(),
                                        config
                                )
                        );
                    } catch (RuntimeException ignored) {
                        resolving = false;
                    }
                }
            });
        } catch (RuntimeException ignored) {
            resolving = false;
            resolveNext(currentGeneration, config);
        }
    }

    private void verifyAndAccept(
            int currentGeneration,
            String origin,
            int port,
            ClickSyncConfig config
    ) {
        boolean verified = authenticatedProbe(origin, config);
        mainHandler.post(() -> {
            if (!isCurrent(currentGeneration)) {
                return;
            }
            if (!verified) {
                resolving = false;
                resolveNext(currentGeneration, config);
                return;
            }
            SharedPreferences preferences = appContext.getSharedPreferences(
                    ClickSyncConfig.PREFERENCES_NAME,
                    Context.MODE_PRIVATE
            );
            boolean saved = preferences.edit()
                    .putString(ClickSyncConfig.KEY_HOST, origin)
                    .putString(ClickSyncConfig.KEY_PORT, String.valueOf(port))
                    .commit();
            if (!saved) {
                resolving = false;
                return;
            }
            stopCurrentDiscovery();
            listener.onVerifiedOrigin(origin);
        });
    }

    private boolean authenticatedProbe(String origin, ClickSyncConfig config) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(
                    origin + "/v1/android/readium/health"
            ).openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(2_500);
            connection.setReadTimeout(2_500);
            connection.setRequestMethod("GET");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("X-Click-Device-Id", config.deviceId);
            connection.setRequestProperty("X-Click-Access-Token", config.accessToken);
            connection.setRequestProperty("Authorization", "Bearer " + config.accessToken);
            int code = connection.getResponseCode();
            if (code != 200) {
                return false;
            }
            try (InputStream input = connection.getInputStream();
                 ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[4_096];
                int read;
                int total = 0;
                while ((read = input.read(buffer)) >= 0) {
                    total += read;
                    if (total > 64 * 1_024) {
                        return false;
                    }
                    output.write(buffer, 0, read);
                }
                JSONObject payload = new JSONObject(
                        output.toString(StandardCharsets.UTF_8.name())
                );
                return payload.optBoolean("ok", false)
                        && "click.android.readium.health.v1".equals(
                                payload.optString("schema", "")
                        )
                        && "available".equals(payload.optString("reader_api", ""));
            }
        } catch (Throwable ignored) {
            return false;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private void stopCurrentDiscovery() {
        mainHandler.removeCallbacks(discoveryTimeout);
        NsdManager.DiscoveryListener active = discoveryListener;
        discoveryListener = null;
        if (active != null && nsdManager != null) {
            try {
                nsdManager.stopServiceDiscovery(active);
            } catch (RuntimeException ignored) {
                // The platform may already have stopped a failed discovery.
            }
        }
    }

    private boolean isCurrent(int currentGeneration) {
        return started && currentGeneration == generation;
    }

    private static boolean serviceTypeMatches(String value) {
        String normalized = value == null ? "" : value.trim().toLowerCase();
        return normalized.equals(SERVICE_TYPE)
                || normalized.equals(SERVICE_TYPE + "local.");
    }

    private static boolean hasExpectedContract(NsdServiceInfo serviceInfo) {
        try {
            Map<String, byte[]> attributes = serviceInfo.getAttributes();
            byte[] contract = attributes.get("contract");
            byte[] transport = attributes.get("transport");
            return contract != null
                    && SERVICE_CONTRACT.equals(new String(contract, StandardCharsets.UTF_8))
                    && transport != null
                    && "http".equals(new String(transport, StandardCharsets.UTF_8));
        } catch (RuntimeException ignored) {
            return false;
        }
    }

    private static String resolvedPrivateOrigin(NsdServiceInfo serviceInfo) {
        if (Build.VERSION.SDK_INT >= 34) {
            for (InetAddress address : serviceInfo.getHostAddresses()) {
                String origin = ClickLanEndpointPolicy.originFor(
                        address == null ? "" : address.getHostAddress(),
                        serviceInfo.getPort()
                );
                if (!origin.isEmpty()) {
                    return origin;
                }
            }
            return "";
        }
        InetAddress address = serviceInfo.getHost();
        return ClickLanEndpointPolicy.originFor(
                address == null ? "" : address.getHostAddress(),
                serviceInfo.getPort()
        );
    }
}
