package com.click.shell;

import android.content.Context;

import androidx.work.BackoffPolicy;
import androidx.work.Constraints;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.WorkManager;

import java.util.concurrent.TimeUnit;

/** Event-driven unique import work with WorkManager backoff; there is no periodic polling. */
final class BookImportScheduler {
    static final String UNIQUE_WORK_NAME = "click.book.import.v1";
    static final String TAG_IMPORT = "click.book.import";

    private BookImportScheduler() {
    }

    static void enqueue(Context context) {
        Context appContext = context.getApplicationContext();
        WorkManager.getInstance(appContext).enqueueUniqueWork(
                UNIQUE_WORK_NAME,
                ExistingWorkPolicy.APPEND_OR_REPLACE,
                request()
        );
    }

    static void continuePending(Context context) {
        enqueue(context);
    }

    static boolean enqueuePending(Context context) {
        Context appContext = context.getApplicationContext();
        try (ClickNativeStore store = new ClickNativeStore(appContext)) {
            if (store.pendingBookImportCount() <= 0) {
                return false;
            }
        }
        enqueue(appContext);
        return true;
    }

    static OneTimeWorkRequest request() {
        Constraints constraints = new Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build();
        return new OneTimeWorkRequest.Builder(BookImportWorker.class)
                .setConstraints(constraints)
                .setBackoffCriteria(
                        BackoffPolicy.EXPONENTIAL,
                        OneTimeWorkRequest.MIN_BACKOFF_MILLIS,
                        TimeUnit.MILLISECONDS
                )
                .addTag(TAG_IMPORT)
                .build();
    }
}
