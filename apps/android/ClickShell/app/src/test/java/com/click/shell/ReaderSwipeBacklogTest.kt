package com.click.shell

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ReaderSwipeBacklogTest {
    @Test
    fun independentSwipesProduceOneStableTargetDelta() {
        val backlog = ReaderSwipeBacklog()
        repeat(10) { assertTrue(backlog.record("swipe-$it", 1)) }
        assertFalse(backlog.record("swipe-9", 1))
        assertEquals(10, backlog.netDirection())
        assertEquals(10, backlog.size())

        backlog.clear()
        assertEquals(0, backlog.size())
    }
}
