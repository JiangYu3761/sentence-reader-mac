package com.click.shell;

import android.content.Context;

import androidx.work.BackoffPolicy;
import androidx.work.Constraints;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.WorkManager;

import java.util.concurrent.TimeUnit;

/**
 * Event-driven Tingle upload scheduling.
 *
 * <p>There is intentionally no periodic request, timer, polling loop, or credential in Work
     * Data. Android wakes this unique one-shot job when a connected network is available.</p>
 */
final class TingleUploadScheduler {
    static final String UNIQUE_WORK_NAME = "click.tingle.upload.v1";
    static final String TAG_UPLOAD = "click.tingle.upload";
    private static final long BACKOFF_SECONDS = 30L;

    private TingleUploadScheduler() {
    }

    static void enqueue(Context context) {
        Context appContext = context.getApplicationContext();
        WorkManager.getInstance(appContext).enqueueUniqueWork(
                UNIQUE_WORK_NAME,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                request()
        );
    }

    /**
     * Appends one more bounded upload only from the worker that just completed successfully.
     *
     * <p>Both capture events and continuations append durably. This closes the KEEP race where a
     * capture arriving during the prior Worker's final snapshot could otherwise lose its wakeup.</p>
     */
    static void continuePending(Context context) {
        Context appContext = context.getApplicationContext();
        WorkManager.getInstance(appContext).enqueueUniqueWork(
                UNIQUE_WORK_NAME,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                request()
        );
    }

    static OneTimeWorkRequest request() {
        Constraints constraints = new Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build();
        return new OneTimeWorkRequest.Builder(TingleUploadWorker.class)
                .setConstraints(constraints)
                .setBackoffCriteria(
                        BackoffPolicy.EXPONENTIAL,
                        BACKOFF_SECONDS,
                        TimeUnit.SECONDS
                )
                .addTag(TAG_UPLOAD)
                .build();
    }
}
