package com.click.shell

/** Tracks independent swipes that Readium may drop while its page animation is settling. */
class ReaderSwipeBacklog {
    private data class Swipe(val id: String, val direction: Int)

    private val pending = java.util.ArrayDeque<Swipe>()
    private var lastId = ""

    @Synchronized
    fun record(id: String, direction: Int): Boolean {
        val normalized = direction.coerceIn(-1, 1)
        if (id.isBlank() || normalized == 0 || id == lastId) return false
        lastId = id
        if (pending.size >= MAX_PENDING) return false
        pending.addLast(Swipe(id, normalized))
        return true
    }

    @Synchronized
    fun netDirection(): Int = pending.sumOf { it.direction }

    @Synchronized
    fun size(): Int = pending.size

    @Synchronized
    fun clear() {
        pending.clear()
        lastId = ""
    }

    companion object {
        private const val MAX_PENDING = 16
    }
}
