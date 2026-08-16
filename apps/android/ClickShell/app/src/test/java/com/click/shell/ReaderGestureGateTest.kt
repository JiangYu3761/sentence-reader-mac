package com.click.shell

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReaderGestureGateTest {
    @Test
    fun samePhysicalGestureOnlyTurnsOnce() {
        val gate = ReaderGestureGate()
        assertTrue(gate.request("gesture-1", 1))
        assertFalse(gate.request("gesture-1", 1))
        assertEquals(null, gate.complete())
        assertEquals(0, gate.pendingCount())
    }

    @Test
    fun tenIndependentGesturesAreNotLostBehindAnimation() {
        val gate = ReaderGestureGate()
        assertTrue(gate.request("gesture-0", 1))
        for (index in 1 until 10) assertFalse(gate.request("gesture-$index", 1))
        val queued = mutableListOf<Int>()
        while (true) {
            val direction = gate.complete() ?: break
            queued += direction
        }
        assertEquals(9, queued.size)
        assertTrue(queued.all { it == 1 })
        assertEquals(0, gate.pendingCount())
    }
}
