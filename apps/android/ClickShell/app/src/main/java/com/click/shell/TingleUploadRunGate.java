package com.click.shell;

import java.util.concurrent.atomic.AtomicBoolean;

/** Process-local guard shared by foreground and WorkManager Tingle upload callers. */
final class TingleUploadRunGate {
    private static final AtomicBoolean RUNNING = new AtomicBoolean(false);

    private TingleUploadRunGate() {
    }

    static Lease tryAcquire() {
        return RUNNING.compareAndSet(false, true) ? new Lease() : null;
    }

    static boolean isRunning() {
        return RUNNING.get();
    }

    static final class Lease implements AutoCloseable {
        private final AtomicBoolean closed = new AtomicBoolean(false);

        @Override
        public void close() {
            if (closed.compareAndSet(false, true)) {
                RUNNING.set(false);
            }
        }
    }
}
