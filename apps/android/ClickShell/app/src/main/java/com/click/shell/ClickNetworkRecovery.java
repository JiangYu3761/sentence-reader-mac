package com.click.shell;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Handler;
import android.os.Looper;

import java.util.concurrent.Executor;

/**
 * Turns a foreground/network recovery event into bounded unique WorkManager wake-ups.
 *
 * <p>The callback never performs I/O. A short main-thread debounce collapses the usual
 * onAvailable/capabilities burst, then the Activity's existing serial executor performs only
 * lightweight durable-pending checks and scheduler calls.</p>
 */
final class ClickNetworkRecovery {
    static final long DEBOUNCE_MILLIS = 2_000L;

    private final Context appContext;
    private final Executor serialExecutor;
    private final ConnectivityManager connectivityManager;
    private final Handler mainHandler;
    private final Runnable onNetworkRecovered;
    private final ConnectivityManager.NetworkCallback networkCallback;
    private final Runnable debouncedWakeup;

    private boolean started;
    private boolean callbackRegistered;
    private Network observedNetwork;
    private boolean observedInternetCapability;
    private volatile int activeGeneration;

    ClickNetworkRecovery(
            Context context,
            Executor serialExecutor,
            Runnable onNetworkRecovered
    ) {
        this.appContext = context.getApplicationContext();
        this.serialExecutor = serialExecutor;
        this.onNetworkRecovered = onNetworkRecovered;
        this.connectivityManager = appContext.getSystemService(ConnectivityManager.class);
        this.mainHandler = new Handler(Looper.getMainLooper());
        this.debouncedWakeup = this::dispatchRecoveryWakeup;
        this.networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                boolean isNewNetwork =
                        observedNetwork == null || !observedNetwork.equals(network);
                observedNetwork = network;
                if (isNewNetwork) {
                    observedInternetCapability = false;
                    scheduleDebouncedWakeup();
                }
            }

            @Override
            public void onCapabilitiesChanged(
                    Network network,
                    NetworkCapabilities networkCapabilities
            ) {
                boolean hasInternet = networkCapabilities.hasCapability(
                        NetworkCapabilities.NET_CAPABILITY_INTERNET
                );
                boolean isNewNetwork =
                        observedNetwork == null || !observedNetwork.equals(network);
                boolean recovered = hasInternet
                        && (isNewNetwork || !observedInternetCapability);
                observedNetwork = network;
                observedInternetCapability = hasInternet;
                if (recovered) {
                    scheduleDebouncedWakeup();
                }
            }

            @Override
            public void onLost(Network network) {
                if (network.equals(observedNetwork)) {
                    observedNetwork = null;
                    observedInternetCapability = false;
                }
            }
        };
    }

    void start() {
        if (started) {
            return;
        }
        started = true;
        activeGeneration += 1;
        if (connectivityManager == null) {
            observedNetwork = null;
            observedInternetCapability = false;
            return;
        }
        // MainActivity already emits its normal foreground one-shots. Seed the current default
        // network so Android's immediate registration callbacks do not append duplicates.
        try {
            observedNetwork = connectivityManager.getActiveNetwork();
            NetworkCapabilities currentCapabilities = observedNetwork == null
                    ? null
                    : connectivityManager.getNetworkCapabilities(observedNetwork);
            observedInternetCapability = currentCapabilities != null
                    && currentCapabilities.hasCapability(
                            NetworkCapabilities.NET_CAPABILITY_INTERNET
                    );
            connectivityManager.registerDefaultNetworkCallback(networkCallback, mainHandler);
            callbackRegistered = true;
        } catch (RuntimeException ignored) {
            observedNetwork = null;
            observedInternetCapability = false;
            // MainActivity's normal foreground wake-up remains durable if callback registration is
            // unavailable on a vendor build. There is no fallback poller.
        }
    }

    void stop() {
        if (!started && !callbackRegistered) {
            return;
        }
        started = false;
        activeGeneration += 1;
        mainHandler.removeCallbacks(debouncedWakeup);
        observedNetwork = null;
        observedInternetCapability = false;
        if (callbackRegistered && connectivityManager != null) {
            try {
                connectivityManager.unregisterNetworkCallback(networkCallback);
            } catch (RuntimeException ignored) {
                // A duplicate platform-side unregister should not crash Activity teardown.
            }
        }
        callbackRegistered = false;
    }

    private void scheduleDebouncedWakeup() {
        if (!started) {
            return;
        }
        mainHandler.removeCallbacks(debouncedWakeup);
        mainHandler.postDelayed(debouncedWakeup, DEBOUNCE_MILLIS);
    }

    private void dispatchRecoveryWakeup() {
        if (!started) {
            return;
        }
        if (onNetworkRecovered != null) {
            onNetworkRecovered.run();
        }
        int generation = activeGeneration;
        try {
            serialExecutor.execute(() -> enqueueRecoveryWork(generation));
        } catch (RuntimeException ignored) {
            // Activity teardown can reject a task after onStop; the next start is a fresh event.
        }
    }

    private void enqueueRecoveryWork(int generation) {
        if (!started || generation != activeGeneration) {
            return;
        }

        ClickSyncScheduler.enqueueMetadata(appContext);
        ClickSyncScheduler.replaceMetadataRepairFromStore(appContext);
        BookImportScheduler.enqueuePending(appContext);

        if (ClickSyncConfig.load(appContext).isReady()
                && new TingleCaptureRepository(appContext).pendingCount() > 0) {
            TingleUploadScheduler.enqueue(appContext);
        }
    }
}
