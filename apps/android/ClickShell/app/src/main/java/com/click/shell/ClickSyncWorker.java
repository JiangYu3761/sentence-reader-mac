package com.click.shell;

import android.content.Context;

import androidx.annotation.NonNull;
import androidx.work.Data;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

/** Headless WorkManager bridge. All protocol and persistence work remains in ClickSyncEngine. */
public final class ClickSyncWorker extends Worker {
    private static final String OUTPUT_STATUS = "status";
    private static final String OUTPUT_MESSAGE = "message";
    private static final String OUTPUT_OPERATIONS = "operations_uploaded";
    private static final String OUTPUT_CHANGES = "changes_applied";
    private static final String OUTPUT_ASSETS = "assets_downloaded";
    private static final String OUTPUT_BOOKS_FAILED = "books_failed";
    static final int MAX_ASSETS_PER_RUN = 1;

    public ClickSyncWorker(
            @NonNull Context appContext,
            @NonNull WorkerParameters workerParameters
    ) {
        super(appContext, workerParameters);
    }

    @NonNull
    @Override
    public Result doWork() {
        String mode = getInputData().getString(ClickSyncScheduler.INPUT_MODE);
        String lane = getInputData().getString(ClickSyncScheduler.INPUT_LANE);
        String bookId = getInputData().getString(ClickSyncScheduler.INPUT_BOOK_ID);
        if (!ClickSyncScheduler.MODE_METADATA.equals(mode)
                && !ClickSyncScheduler.MODE_METADATA_REPAIR.equals(mode)
                && !ClickSyncScheduler.MODE_ASSETS.equals(mode)) {
            return Result.failure(errorData("invalid_mode", "未知同步模式"));
        }
        if (!validModeLane(mode, lane)) {
            return Result.failure(errorData("invalid_lane", "未知同步通道"));
        }
        if (!validBookScope(lane, bookId)) {
            return Result.failure(errorData("invalid_book_scope", "单本同步范围无效"));
        }

        ClickSyncEngine.Outcome outcome;
        try (ClickSyncEngine engine = new ClickSyncEngine(getApplicationContext())) {
            if (ClickSyncScheduler.MODE_METADATA.equals(mode)) {
                outcome = engine.runMetadataIncremental();
            } else if (ClickSyncScheduler.MODE_METADATA_REPAIR.equals(mode)) {
                outcome = engine.runMetadataRepairOnly();
            } else if (ClickSyncScheduler.LANE_EXPLICIT_BOOK_SOURCE.equals(lane)) {
                outcome = engine.runBookSourceAssetOnly(bookId);
            } else {
                outcome = engine.runAssetOnly(MAX_ASSETS_PER_RUN);
            }
        } catch (Throwable error) {
            return Result.failure(errorData("internal_error", error.getClass().getSimpleName()));
        }

        if (isStopped()) {
            return Result.retry();
        }
        if (ClickSyncScheduler.MODE_METADATA.equals(mode)
                && outcome.assetsRemain
                && outcome.status != ClickSyncEngine.Outcome.Status.AUTH_REQUIRED
                && outcome.status != ClickSyncEngine.Outcome.Status.PERMANENT_FAILURE) {
            // A retryable local operation does not block newly discovered remote assets.
            ClickSyncScheduler.enqueueAssets(getApplicationContext());
        }
        if (ClickSyncScheduler.MODE_METADATA.equals(mode)
                && outcome.metadataRepairsRemain
                && outcome.status != ClickSyncEngine.Outcome.Status.AUTH_REQUIRED
                && outcome.status != ClickSyncEngine.Outcome.Status.PERMANENT_FAILURE) {
            ClickSyncScheduler.replaceMetadataRepairFromStore(getApplicationContext());
        }
        Action action = actionForStatus(outcome.status);
        if (action == Action.RETRY) {
            return Result.retry();
        }
        if (action == Action.FAILURE) {
            return Result.failure(outcomeData(outcome));
        }

        if (ClickSyncScheduler.MODE_METADATA.equals(mode)) {
            // Every durable local write appends its own unique-work wake-up. Do not add a
            // completion-tail worker here, which would duplicate real triggers.
        } else if (ClickSyncScheduler.MODE_METADATA_REPAIR.equals(mode)) {
            if (outcome.dueMetadataRepairsRemain) {
                ClickSyncScheduler.continueMetadataRepair(getApplicationContext(), 0L);
            } else if (outcome.metadataRepairsRemain
                    && outcome.nextMetadataRepairAtMillis >= 0L) {
                ClickSyncScheduler.continueMetadataRepair(
                        getApplicationContext(),
                        outcome.nextMetadataRepairAtMillis
                );
            } else if (outcome.metadataRepairsRemain) {
                return Result.retry();
            }
        } else if (ClickSyncScheduler.LANE_EXPLICIT_BOOK_SOURCE.equals(lane)) {
            // This one-shot lane is scoped to one source row. A transient row remains durable for
            // the normal Wi-Fi backlog; never continue or release unrelated asset work from here.
        } else if (outcome.dueAssetsRemain) {
            if (ClickSyncScheduler.LANE_CHARGING_BACKLOG.equals(lane)) {
                // A backlog may drain automatically only while charging. Immediate and delayed
                // event lanes process one item, then hand the remaining work to this lane.
                ClickSyncScheduler.continueAssets(getApplicationContext(), lane, 0L);
            } else {
                ClickSyncScheduler.enqueueChargingAssets(getApplicationContext());
            }
        } else if (outcome.assetsRemain && outcome.nextAssetAttemptAtMillis >= 0L) {
            if (ClickSyncScheduler.LANE_DELAYED_RETRY.equals(lane)) {
                ClickSyncScheduler.continueAssets(
                        getApplicationContext(),
                        lane,
                        outcome.nextAssetAttemptAtMillis
                );
            } else {
                // A future retry lives on a separate replaceable lane, so it can never block a
                // newly discovered due-now asset on the immediate KEEP lane.
                ClickSyncScheduler.replaceDelayedAssetsFromStore(getApplicationContext());
            }
        } else if (outcome.assetsRemain) {
            return Result.retry();
        }
        return Result.success(outcomeData(outcome));
    }

    static Action actionForStatus(ClickSyncEngine.Outcome.Status status) {
        if (status == ClickSyncEngine.Outcome.Status.BUSY
                || status == ClickSyncEngine.Outcome.Status.RETRYABLE_FAILURE) {
            return Action.RETRY;
        }
        if (status == ClickSyncEngine.Outcome.Status.AUTH_REQUIRED
                || status == ClickSyncEngine.Outcome.Status.PERMANENT_FAILURE) {
            return Action.FAILURE;
        }
        return Action.SUCCESS;
    }

    static boolean validModeLane(String mode, String lane) {
        if (ClickSyncScheduler.MODE_METADATA.equals(mode)) {
            return ClickSyncScheduler.LANE_IMMEDIATE.equals(lane);
        }
        if (ClickSyncScheduler.MODE_METADATA_REPAIR.equals(mode)) {
            return ClickSyncScheduler.LANE_METADATA_REPAIR.equals(lane);
        }
        return ClickSyncScheduler.MODE_ASSETS.equals(mode)
                && (ClickSyncScheduler.LANE_IMMEDIATE.equals(lane)
                || ClickSyncScheduler.LANE_DELAYED_RETRY.equals(lane)
                || ClickSyncScheduler.LANE_CHARGING_BACKLOG.equals(lane)
                || ClickSyncScheduler.LANE_EXPLICIT_BOOK_SOURCE.equals(lane));
    }

    static boolean validBookScope(String lane, String bookId) {
        boolean explicitBookLane = ClickSyncScheduler.LANE_EXPLICIT_BOOK_SOURCE.equals(lane);
        boolean hasBookId = bookId != null && !bookId.trim().isEmpty();
        return explicitBookLane == hasBookId;
    }

    private Data outcomeData(ClickSyncEngine.Outcome outcome) {
        return new Data.Builder()
                .putString(OUTPUT_STATUS, outcome.status.name().toLowerCase())
                .putString(OUTPUT_MESSAGE, shortText(outcome.message))
                .putInt(OUTPUT_OPERATIONS, outcome.operationsUploaded)
                .putInt(OUTPUT_CHANGES, outcome.changesApplied)
                .putInt(OUTPUT_ASSETS, outcome.assetsDownloaded)
                .putInt(OUTPUT_BOOKS_FAILED, outcome.booksFailed)
                .build();
    }

    private Data errorData(String status, String message) {
        return new Data.Builder()
                .putString(OUTPUT_STATUS, status)
                .putString(OUTPUT_MESSAGE, shortText(message))
                .build();
    }

    private String shortText(String value) {
        String clean = value == null ? "" : value.trim();
        return clean.length() > 240 ? clean.substring(0, 240) : clean;
    }

    enum Action {
        SUCCESS,
        RETRY,
        FAILURE
    }
}
