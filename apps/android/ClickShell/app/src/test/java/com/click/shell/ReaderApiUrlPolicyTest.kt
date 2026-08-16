package com.click.shell

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class ReaderApiUrlPolicyTest {
    @Test
    fun resolvesRelativeAndSameOriginUrls() {
        val base = "http://click-mac.tailnet.ts.net:18180"
        assertEquals(
            "$base/v1/android/sync/full",
            ReaderApiUrlPolicy.resolve(base, "/v1/android/sync/full"),
        )
        assertEquals(
            "$base/v1/android/tts/audio",
            ReaderApiUrlPolicy.resolve(base, "v1/android/tts/audio"),
        )
        assertEquals(
            "$base/v1/android/books/one/epub",
            ReaderApiUrlPolicy.resolve(base, "$base/v1/android/books/one/epub"),
        )
    }

    @Test
    fun rejectsCredentialExfiltrationOriginsAndUnsafeSchemes() {
        val base = "https://click-mac.tailnet.ts.net"
        for (candidate in listOf(
            "https://evil.example/audio",
            "//evil.example/audio",
            "http://click-mac.tailnet.ts.net/audio",
            "https://click-mac.tailnet.ts.net:444/audio",
            "https://user@click-mac.tailnet.ts.net/audio",
            "file:///tmp/audio",
            "https://click-mac.tailnet.ts.net/audio#secret",
        )) {
            assertThrows(RuntimeException::class.java) {
                ReaderApiUrlPolicy.resolve(base, candidate)
            }
        }
    }

    @Test
    fun comparesEffectivePorts() {
        assertTrue(ReaderApiUrlPolicy.sameOrigin("http://click.local", "http://click.local:80/health"))
        assertTrue(ReaderApiUrlPolicy.sameOrigin("https://click.local", "https://click.local:443/health"))
        assertFalse(ReaderApiUrlPolicy.sameOrigin("http://click.local", "http://click.local:18180/health"))
    }
}
