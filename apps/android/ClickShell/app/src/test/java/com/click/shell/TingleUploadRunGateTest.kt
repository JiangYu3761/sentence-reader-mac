package com.click.shell

import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class TingleUploadRunGateTest {
    @Test
    fun foregroundAndWorkerCannotUploadAtTheSameTime() {
        val first = TingleUploadRunGate.tryAcquire()
        assertNotNull(first)
        assertTrue(TingleUploadRunGate.isRunning())
        assertNull(TingleUploadRunGate.tryAcquire())

        first!!.close()
        first.close()
        assertFalse(TingleUploadRunGate.isRunning())

        val second = TingleUploadRunGate.tryAcquire()
        assertNotNull(second)
        second!!.close()
        assertFalse(TingleUploadRunGate.isRunning())
    }
}
