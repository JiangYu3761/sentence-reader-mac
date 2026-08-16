package com.click.shell

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ClickAssetRefreshBackoffTest {
    @Test
    fun failedAssetsUseBoundedExponentialBackoff() {
        assertEquals(60_000L, ClickNativeStore.assetRetryDelayMillis(1))
        assertEquals(120_000L, ClickNativeStore.assetRetryDelayMillis(2))
        assertEquals(240_000L, ClickNativeStore.assetRetryDelayMillis(3))

        var previous = 0L
        for (retryCount in 1..30) {
            val delay = ClickNativeStore.assetRetryDelayMillis(retryCount)
            assertTrue(delay >= previous)
            assertTrue(delay <= 6L * 60L * 60L * 1000L)
            previous = delay
        }
        assertEquals(6L * 60L * 60L * 1000L, previous)
    }
}
