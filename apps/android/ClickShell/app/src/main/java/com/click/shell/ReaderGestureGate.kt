package com.click.shell

/** Keeps one physical gesture to one page while allowing distinct later gestures to queue. */
class ReaderGestureGate {
    private var busy = false
    private var lastGestureId = ""
    private val queuedDirections = java.util.ArrayDeque<Int>()

    @Synchronized
    fun request(gestureId: String, direction: Int): Boolean {
        val normalized = direction.coerceIn(-1, 1)
        if (normalized == 0 || gestureId.isBlank() || gestureId == lastGestureId) return false
        lastGestureId = gestureId
        if (!busy) {
            busy = true
            return true
        }
        if (queuedDirections.size < MAX_QUEUED_GESTURES) queuedDirections.addLast(normalized)
        return false
    }

    @Synchronized
    fun complete(): Int? {
        val queued = queuedDirections.pollFirst()
        if (queued == null) busy = false
        return queued
    }

    @Synchronized
    fun clear() {
        busy = false
        lastGestureId = ""
        queuedDirections.clear()
    }

    @Synchronized
    fun pendingCount(): Int = queuedDirections.size + if (busy) 1 else 0

    companion object {
        private const val MAX_QUEUED_GESTURES = 12
    }
}
