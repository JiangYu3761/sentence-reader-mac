package com.click.shell;

import android.content.Context;

import androidx.work.BackoffPolicy;
import androidx.work.Constraints;
import androidx.work.Data;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.WorkManager;

import java.nio.charset.StandardCharsets;
import java.util.concurrent.TimeUnit;
import java.util.UUID;

/** Event-driven one-time scheduling only. There is intentionally no periodic work. */
final class ClickSyncScheduler {
    static final String UNIQUE_METADATA = "click.sync.metadata.v1";
    static final String UNIQUE_METADATA_REPAIR = "click.sync.metadata.repair.v1";
    static final String UNIQUE_ASSETS_IMMEDIATE = "click.sync.assets.immediate.v2";
    static final String UNIQUE_ASSETS_DELAYED = "click.sync.assets.delayed.v2";
    static final String UNIQUE_ASSETS_CHARGING = "click.sync.assets.charging.v1";
    static final String UNIQUE_BOOK_SOURCE_PREFIX = "click.sync.book-source.v1.";
    static final String TAG_SYNC = "click.sync";
    static final String INPUT_MODE = "mode";
    static final String INPUT_LANE = "lane";
    static final String INPUT_BOOK_ID = "book_id";
    static final String MODE_METADATA = "metadata";
    static final String MODE_METADATA_REPAIR = "metadata_repair";
    static final String MODE_ASSETS = "assets";
    static final String LANE_IMMEDIATE = "immediate";
    static final String LANE_DELAYED_RETRY = "delayed_retry";
    static final String LANE_CHARGING_BACKLOG = "charging_backlog";
    static final String LANE_METADATA_REPAIR = "metadata_repair";
    static final String LANE_EXPLICIT_BOOK_SOURCE = "explicit_book_source";

    private static final long METADATA_BACKOFF_SECONDS = 30L;
    private static final long ASSET_BACKOFF_SECONDS = 60L;

    private ClickSyncScheduler() {
    }

    static void enqueueMetadata(Context context) {
        Context appContext = context.getApplicationContext();
        WorkManager.getInstance(appContext).enqueueUniqueWork(
                UNIQUE_METADATA,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                metadataRequest(MODE_METADATA, LANE_IMMEDIATE, 0L)
        );
    }

    static void enqueueAssets(Context context) {
        Context appContext = context.getApplicationContext();
        WorkManager.getInstance(appContext).enqueueUniqueWork(
                UNIQUE_ASSETS_IMMEDIATE,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                assetRequest(
                        ClickSyncConfig.allowMeteredAssets(appContext),
                        LANE_IMMEDIATE,
                        0L
                )
        );
        replaceDelayedAssetsFromStore(appContext);
    }

    static void enqueueExplicitBookSource(
            Context context,
            String bookId,
            boolean allowMetered
    ) {
        String cleanBookId = clean(bookId);
        if (cleanBookId.isEmpty()) {
            return;
        }
        Context appContext = context.getApplicationContext();
        WorkManager.getInstance(appContext).enqueueUniqueWork(
                bookSourceWorkName(cleanBookId),
                ExistingWorkPolicy.REPLACE,
                assetRequest(
                        allowMetered,
                        LANE_EXPLICIT_BOOK_SOURCE,
                        0L,
                        cleanBookId
                )
        );
    }

    static boolean updateMeteredAssetPolicy(Context context, boolean allowMetered) {
        Context appContext = context.getApplicationContext();
        boolean previous = ClickSyncConfig.allowMeteredAssets(appContext);
        if (!ClickSyncConfig.setAllowMeteredAssets(appContext, allowMetered)) {
            return false;
        }
        try {
            WorkManager workManager = WorkManager.getInstance(appContext);
            workManager.enqueueUniqueWork(
                    UNIQUE_ASSETS_IMMEDIATE,
                    ExistingWorkPolicy.REPLACE,
                    assetRequest(allowMetered, LANE_IMMEDIATE, 0L)
            );
            long now = System.currentTimeMillis();
            long earliest = earliestAssetAttemptAt(appContext);
            if (earliest >= 0L) {
                long delayMillis = delayUntil(now, earliest);
                workManager.enqueueUniqueWork(
                        UNIQUE_ASSETS_CHARGING,
                        ExistingWorkPolicy.REPLACE,
                        assetRequest(
                                allowMetered,
                                LANE_CHARGING_BACKLOG,
                                delayMillis
                        )
                );
                if (earliest > now) {
                    workManager.enqueueUniqueWork(
                            UNIQUE_ASSETS_DELAYED,
                            ExistingWorkPolicy.REPLACE,
                            assetRequest(
                                    allowMetered,
                                    LANE_DELAYED_RETRY,
                                    delayMillis
                            )
                    );
                } else {
                    workManager.cancelUniqueWork(UNIQUE_ASSETS_DELAYED);
                }
            } else {
                workManager.cancelUniqueWork(UNIQUE_ASSETS_DELAYED);
                workManager.cancelUniqueWork(UNIQUE_ASSETS_CHARGING);
            }
            return true;
        } catch (Throwable ignored) {
            ClickSyncConfig.setAllowMeteredAssets(appContext, previous);
            return false;
        }
    }

    static void enqueueChargingAssets(Context context) {
        Context appContext = context.getApplicationContext();
        WorkManager.getInstance(appContext).enqueueUniqueWork(
                UNIQUE_ASSETS_CHARGING,
                ExistingWorkPolicy.KEEP,
                assetRequest(
                        ClickSyncConfig.allowMeteredAssets(appContext),
                        LANE_CHARGING_BACKLOG,
                        0L
                )
        );
    }

    static void continueAssets(
            Context context,
            String lane,
            long nextAttemptAtMillis
    ) {
        Context appContext = context.getApplicationContext();
        long delayMillis = delayUntil(
                System.currentTimeMillis(),
                nextAttemptAtMillis
        );
        boolean delayedLane = LANE_DELAYED_RETRY.equals(lane);
        if (!delayedLane && !LANE_CHARGING_BACKLOG.equals(lane)) {
            return;
        }
        WorkManager.getInstance(appContext).enqueueUniqueWork(
                delayedLane ? UNIQUE_ASSETS_DELAYED : UNIQUE_ASSETS_CHARGING,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                assetRequest(
                        ClickSyncConfig.allowMeteredAssets(appContext),
                        lane,
                        delayMillis
                )
        );
    }

    static void replaceDelayedAssetsFromStore(Context context) {
        Context appContext = context.getApplicationContext();
        long earliest = earliestAssetAttemptAt(appContext);
        long now = System.currentTimeMillis();
        if (earliest <= now) {
            return;
        }
        WorkManager.getInstance(appContext).enqueueUniqueWork(
                UNIQUE_ASSETS_DELAYED,
                ExistingWorkPolicy.REPLACE,
                assetRequest(
                        ClickSyncConfig.allowMeteredAssets(appContext),
                        LANE_DELAYED_RETRY,
                        delayUntil(now, earliest)
                )
        );
    }

    static void replaceMetadataRepairFromStore(Context context) {
        Context appContext = context.getApplicationContext();
        long earliest = earliestMetadataRepairAttemptAt(appContext);
        if (earliest < 0L) {
            return;
        }
        long now = System.currentTimeMillis();
        WorkManager.getInstance(appContext).enqueueUniqueWork(
                UNIQUE_METADATA_REPAIR,
                ExistingWorkPolicy.REPLACE,
                metadataRequest(
                        MODE_METADATA_REPAIR,
                        LANE_METADATA_REPAIR,
                        delayUntil(now, earliest)
                )
        );
    }

    static void continueMetadataRepair(Context context, long nextAttemptAtMillis) {
        Context appContext = context.getApplicationContext();
        WorkManager.getInstance(appContext).enqueueUniqueWork(
                UNIQUE_METADATA_REPAIR,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                metadataRequest(
                        MODE_METADATA_REPAIR,
                        LANE_METADATA_REPAIR,
                        delayUntil(System.currentTimeMillis(), nextAttemptAtMillis)
                )
        );
    }

    static OneTimeWorkRequest metadataRequest(
            String mode,
            String lane,
            long initialDelayMillis
    ) {
        Constraints constraints = new Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build();
        OneTimeWorkRequest.Builder builder = new OneTimeWorkRequest.Builder(
                ClickSyncWorker.class
        )
                .setInputData(modeData(mode, lane))
                .setConstraints(constraints)
                .setBackoffCriteria(
                        BackoffPolicy.EXPONENTIAL,
                        METADATA_BACKOFF_SECONDS,
                        TimeUnit.SECONDS
                )
                .addTag(TAG_SYNC);
        if (initialDelayMillis > 0L) {
            builder.setInitialDelay(initialDelayMillis, TimeUnit.MILLISECONDS);
        }
        return builder.build();
    }

    static OneTimeWorkRequest assetRequest(
            boolean allowMeteredAssets,
            String lane,
            long initialDelayMillis
    ) {
        return assetRequest(allowMeteredAssets, lane, initialDelayMillis, "");
    }

    private static OneTimeWorkRequest assetRequest(
            boolean allowMeteredAssets,
            String lane,
            long initialDelayMillis,
            String bookId
    ) {
        Constraints constraints = new Constraints.Builder()
                .setRequiredNetworkType(assetNetworkType(allowMeteredAssets))
                .setRequiresStorageNotLow(true)
                .setRequiresCharging(assetLaneRequiresCharging(lane))
                .build();
        OneTimeWorkRequest.Builder builder = new OneTimeWorkRequest.Builder(
                ClickSyncWorker.class
        )
                .setInputData(modeData(MODE_ASSETS, lane, bookId))
                .setConstraints(constraints)
                .setBackoffCriteria(
                        BackoffPolicy.EXPONENTIAL,
                        ASSET_BACKOFF_SECONDS,
                        TimeUnit.SECONDS
                )
                .addTag(TAG_SYNC);
        if (initialDelayMillis > 0L) {
            builder.setInitialDelay(initialDelayMillis, TimeUnit.MILLISECONDS);
        }
        return builder.build();
    }

    static NetworkType assetNetworkType(boolean allowMeteredAssets) {
        return allowMeteredAssets ? NetworkType.CONNECTED : NetworkType.UNMETERED;
    }

    static boolean assetLaneRequiresCharging(String lane) {
        return LANE_CHARGING_BACKLOG.equals(lane);
    }

    static long delayUntil(long nowMillis, long nextAttemptAtMillis) {
        if (nextAttemptAtMillis <= 0L) {
            return 0L;
        }
        return Math.max(0L, nextAttemptAtMillis - Math.max(0L, nowMillis));
    }

    static String bookSourceWorkName(String bookId) {
        String cleanBookId = clean(bookId);
        if (cleanBookId.isEmpty()) {
            return "";
        }
        return UNIQUE_BOOK_SOURCE_PREFIX + UUID.nameUUIDFromBytes(
                cleanBookId.getBytes(StandardCharsets.UTF_8)
        );
    }

    private static long earliestAssetAttemptAt(Context context) {
        try (ClickNativeStore store = new ClickNativeStore(context)) {
            return store.earliestAssetAttemptAt();
        } catch (Throwable ignored) {
            return -1L;
        }
    }

    private static long earliestMetadataRepairAttemptAt(Context context) {
        try (ClickNativeStore store = new ClickNativeStore(context)) {
            return store.earliestMetadataRepairAttemptAt();
        } catch (Throwable ignored) {
            return -1L;
        }
    }

    private static Data modeData(String mode, String lane) {
        return modeData(mode, lane, "");
    }

    private static Data modeData(String mode, String lane, String bookId) {
        // Work Data is persisted by WorkManager. Only non-secret routing data belongs here;
        // endpoint, device ID and access token are loaded inside the worker at execution.
        Data.Builder builder = new Data.Builder()
                .putString(INPUT_MODE, mode)
                .putString(INPUT_LANE, lane);
        String cleanBookId = clean(bookId);
        if (!cleanBookId.isEmpty()) {
            builder.putString(INPUT_BOOK_ID, cleanBookId);
        }
        return builder.build();
    }

    private static String clean(String value) {
        return value == null ? "" : value.trim();
    }
}
