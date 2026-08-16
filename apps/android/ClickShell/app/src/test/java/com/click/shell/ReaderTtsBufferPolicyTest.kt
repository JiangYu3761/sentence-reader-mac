package com.click.shell

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReaderTtsBufferPolicyTest {
    @Test
    fun emptyBufferRefillsToNinetyMinutes() {
        val decision = ReaderTtsBufferPolicy.decide(0L, reachedBookEnd = false)
        assertTrue(decision.shouldRefill)
        assertEquals(ReaderTtsBufferPolicy.HIGH_WATER_MS, decision.missingDurationMs)
    }

    @Test
    fun fiftyNineMinutesRefillsButSixtyDoesNot() {
        assertTrue(
            ReaderTtsBufferPolicy.decide(59L * 60L * 1_000L, reachedBookEnd = false).shouldRefill,
        )
        assertFalse(
            ReaderTtsBufferPolicy.decide(60L * 60L * 1_000L, reachedBookEnd = false).shouldRefill,
        )
    }

    @Test
    fun bookEndDoesNotPretendMoreAudioCanBeGenerated() {
        val decision = ReaderTtsBufferPolicy.decide(12L * 60L * 1_000L, reachedBookEnd = true)
        assertFalse(decision.shouldRefill)
        assertEquals(0L, decision.missingDurationMs)
    }

    @Test
    fun fasterSpeechShortensEstimatedDuration() {
        val normal = ReaderTtsBufferPolicy.estimatedDurationMs("这是用于估算朗读时长的一句话。", 100)
        val fast = ReaderTtsBufferPolicy.estimatedDurationMs("这是用于估算朗读时长的一句话。", 150)
        assertTrue(normal > fast)
        assertTrue(fast > 0L)
    }
}
