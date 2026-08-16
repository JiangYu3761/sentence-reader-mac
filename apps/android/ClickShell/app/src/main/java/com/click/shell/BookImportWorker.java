package com.click.shell;

import android.content.Context;

import androidx.annotation.NonNull;
import androidx.work.Data;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

/** One durable original per Worker; auth waits for re-pairing and transport uses bounded backoff. */
public final class BookImportWorker extends Worker {
    private static final String OUTPUT_STATUS = "status";
    private static final String OUTPUT_MESSAGE = "message";
    private static final String OUTPUT_UPLOADED = "uploaded";
    private static final String OUTPUT_PENDING = "pending";

    public BookImportWorker(
            @NonNull Context appContext,
            @NonNull WorkerParameters workerParameters
    ) {
        super(appContext, workerParameters);
    }

    @NonNull
    @Override
    public Result doWork() {
        BookImportRepository.Outcome outcome;
        try (BookImportRepository repository =
                     new BookImportRepository(getApplicationContext())) {
            outcome = repository.uploadOne();
        } catch (Throwable error) {
            return Result.failure(
                    errorData("internal_error", error.getClass().getSimpleName())
            );
        }

        Data output = outcomeData(outcome);
        if (isStopped()) {
            // The durable row remains pending. Do not create a retry loop.
            return Result.success(output);
        }
        if (outcome.uploaded) {
            // The canonical merge has rewritten queued operation IDs and payloads. Only now may
            // ordinary metadata sync expose those operations to the server.
            ClickSyncScheduler.enqueueMetadata(getApplicationContext());
        }
        if ((outcome.status == BookImportRepository.Status.SUCCESS
                || outcome.status == BookImportRepository.Status.PERMANENT_FAILURE)
                && outcome.pending > 0) {
            // Continue only after a handled item. Transient/auth failures wait for a real event.
            BookImportScheduler.continuePending(getApplicationContext());
        }
        if (outcome.status == BookImportRepository.Status.AUTH_REQUIRED) {
            return Result.failure(output);
        }
        if (outcome.status == BookImportRepository.Status.RETRYABLE_FAILURE) {
            // WorkManager owns the bounded exponential backoff. The durable row remains the source
            // of truth, and no timer or periodic worker is introduced.
            return Result.retry();
        }
        return Result.success(output);
    }

    private Data outcomeData(BookImportRepository.Outcome outcome) {
        return new Data.Builder()
                .putString(
                        OUTPUT_STATUS,
                        outcome.status.name().toLowerCase(java.util.Locale.ROOT)
                )
                .putString(OUTPUT_MESSAGE, shortText(outcome.message))
                .putBoolean(OUTPUT_UPLOADED, outcome.uploaded)
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
