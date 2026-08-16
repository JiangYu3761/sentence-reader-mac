package com.click.shell;

import java.util.concurrent.atomic.AtomicBoolean;

/** Process-local final guard behind WorkManager's persistent unique-work guarantee. */
final class ClickSyncRunGate {
    private static final AtomicBoolean RUNNING = new AtomicBoolean(false);

    private ClickSyncRunGate() {
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
