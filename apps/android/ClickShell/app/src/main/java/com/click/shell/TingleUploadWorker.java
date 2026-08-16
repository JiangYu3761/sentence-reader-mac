package com.click.shell;

import android.content.Context;

import androidx.annotation.NonNull;
import androidx.work.Data;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

/** WorkManager bridge; endpoint, device identity and Keystore token load only at execution. */
public final class TingleUploadWorker extends Worker {
    private static final String OUTPUT_STATUS = "status";
    private static final String OUTPUT_MESSAGE = "message";
    private static final String OUTPUT_UPLOADED = "uploaded";
    private static final String OUTPUT_FAILED = "permanently_failed";
    private static final String OUTPUT_PENDING = "pending";

    public TingleUploadWorker(
            @NonNull Context appContext,
            @NonNull WorkerParameters workerParameters
    ) {
        super(appContext, workerParameters);
    }

    @NonNull
    @Override
    public Result doWork() {
        // Work Data intentionally contains no secrets. This loads the current encrypted token
        // from Android Keystore-backed preferences when Android actually executes the job.
        ClickSyncConfig config = ClickSyncConfig.load(getApplicationContext());
        if (!config.isReady()) {
            return Result.failure(errorData("auth_required", "Click 尚未完成设备授权"));
        }

        TingleUploader.Outcome outcome;
        try {
            TingleCaptureRepository repository =
                    new TingleCaptureRepository(getApplicationContext());
            outcome = new TingleUploader(repository, config).uploadPending();
        } catch (Throwable error) {
            return Result.failure(
                    errorData("internal_error", error.getClass().getSimpleName())
            );
        }

        if (isStopped()) {
            return Result.retry();
        }
        Data output = outcomeData(outcome);
        if (outcome.status == TingleUploader.Outcome.Status.BUSY
                || outcome.status == TingleUploader.Outcome.Status.RETRYABLE_FAILURE) {
            return Result.retry();
        }
        if (outcome.status == TingleUploader.Outcome.Status.AUTH_REQUIRED) {
            return Result.failure(output);
        }
        if (outcome.pending > 0) {
            // Each Worker uploads at most one original. Continue the same unique chain rather
            // than holding one process/CPU slice across an unbounded capture backlog.
            TingleUploadScheduler.continuePending(getApplicationContext());
        }
        if (outcome.status == TingleUploader.Outcome.Status.PERMANENT_FAILURE) {
            // The failed capture is terminal in its own manifest. Treat this Worker slice as
            // handled so one corrupt recording cannot poison the unique chain behind it.
            return Result.success(output);
        }
        return Result.success(output);
    }

    private Data outcomeData(TingleUploader.Outcome outcome) {
        return new Data.Builder()
                .putString(OUTPUT_STATUS, outcome.status.name().toLowerCase())
                .putString(OUTPUT_MESSAGE, shortText(outcome.message))
                .putInt(OUTPUT_UPLOADED, outcome.uploaded)
                .putInt(OUTPUT_FAILED, outcome.permanentlyFailed)
                .putInt(OUTPUT_PENDING, outcome.pending)
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
        return clean.length() <= 240 ? clean : clean.substring(0, 240);
    }
}
