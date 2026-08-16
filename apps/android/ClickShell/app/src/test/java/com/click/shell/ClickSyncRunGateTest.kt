package com.click.shell

import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ClickSyncRunGateTest {
    @Test
    fun onlyOneLeaseRunsAndCloseIsIdempotent() {
        val first = ClickSyncRunGate.tryAcquire()
        assertNotNull(first)
        assertTrue(ClickSyncRunGate.isRunning())
        assertNull(ClickSyncRunGate.tryAcquire())

        first!!.close()
        first.close()
        assertFalse(ClickSyncRunGate.isRunning())

        val second = ClickSyncRunGate.tryAcquire()
        assertNotNull(second)
        second!!.close()
        assertFalse(ClickSyncRunGate.isRunning())
    }
}
