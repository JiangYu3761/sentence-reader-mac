package com.click.shell;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

public final class ClickNativeReaderStatusTest {
    @Test
    public void offlineStartupNeverClaimsBackgroundSync() {
        assertEquals(
                "离线阅读",
                ClickNativeReaderActivity.initialLibrarySummary(true, false)
        );
        assertEquals(
                "离线阅读 · 尚未完成 Mac 配对",
                ClickNativeReaderActivity.initialLibrarySummary(false, false)
        );
    }

    @Test
    public void connectedStartupCanDescribeBackgroundSync() {
        assertEquals(
                "本地书架 · 正在后台同步",
                ClickNativeReaderActivity.initialLibrarySummary(true, true)
        );
    }

    @Test
    public void pendingTotalIncludesOperationsAndBookImports() {
        assertEquals(5, ClickNativeReaderActivity.pendingSyncItemCount(1, 4));
        assertEquals(0, ClickNativeReaderActivity.pendingSyncItemCount(-1, -1));
    }
}
