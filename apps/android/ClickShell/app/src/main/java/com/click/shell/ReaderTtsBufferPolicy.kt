package com.click.shell

data class ReaderTtsBufferDecision(
    val shouldRefill: Boolean,
    val targetDurationMs: Long,
    val missingDurationMs: Long,
)

/**
 * Event-driven audio buffer policy.
 *
 * Click refills only after playback changes the available horizon. It never uses a periodic
 * worker or timer to keep the cache warm while the user is not listening.
 */
object ReaderTtsBufferPolicy {
    const val LOW_WATER_MINUTES = 60
    const val HIGH_WATER_MINUTES = 90
    const val LOW_WATER_MS = LOW_WATER_MINUTES * 60L * 1_000L
    const val HIGH_WATER_MS = HIGH_WATER_MINUTES * 60L * 1_000L

    fun decide(bufferedDurationMs: Long, reachedBookEnd: Boolean): ReaderTtsBufferDecision {
        val safe = bufferedDurationMs.coerceAtLeast(0L)
        val shouldRefill = !reachedBookEnd && safe < LOW_WATER_MS
        return ReaderTtsBufferDecision(
            shouldRefill = shouldRefill,
            targetDurationMs = if (shouldRefill) HIGH_WATER_MS else safe,
            missingDurationMs = if (shouldRefill) (HIGH_WATER_MS - safe).coerceAtLeast(0L) else 0L,
        )
    }

    /**
     * Used only to bound how much book text is inspected before real audio durations are known.
     * Chinese audiobook speech is conservatively estimated at 240 characters per minute.
     */
    fun estimatedDurationMs(text: String, ratePercent: Int): Long {
        val characters = ReaderTtsTextNormalizer.normalize(text).count { !it.isWhitespace() }
        if (characters == 0) return 0L
        val safeRate = ratePercent.coerceIn(80, 150)
        val base = characters * 60_000L / 240L
        return (base * 100L / safeRate).coerceAtLeast(600L)
    }
}

data class ReaderTtsLookaheadSegment(
    val text: String,
    val chapterLocator: String,
)

data class ReaderTtsLookaheadPlan(
    val segments: List<ReaderTtsLookaheadSegment>,
    val reachedBookEnd: Boolean,
)

fun interface ReaderTtsLookaheadProvider {
    fun load(currentText: String, estimatedTargetMs: Long, ratePercent: Int): ReaderTtsLookaheadPlan
}
