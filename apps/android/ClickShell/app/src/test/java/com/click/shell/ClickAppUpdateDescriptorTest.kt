package com.click.shell

import org.json.JSONObject
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

class ClickAppUpdateDescriptorTest {
    private val hash = "a".repeat(64)
    private val certificate = "b".repeat(64)

    @Test
    fun acceptsNewerSameOriginArtifact() {
        val descriptor = ClickAppUpdateDescriptor.parse(
            JSONObject()
                .put("ok", true)
                .put("schema", ClickAppUpdateDescriptor.SCHEMA)
                .put("available", true)
                .put("version_code", 9)
                .put("version_name", "0.1.8")
                .put("apk_url", "/v1/android/app-update/click-android-9-aaaaaaaaaaaa/apk")
                .put("apk_bytes", 72_000_000)
                .put("apk_sha256", hash)
                .put("certificate_sha256", certificate)
                .put("min_sdk", 28),
            "https://click-mac.tailnet.ts.net",
        )

        assertTrue(descriptor.available)
        assertTrue(descriptor.isNewerThan(8))
        assertFalse(descriptor.isNewerThan(9))
    }

    @Test
    fun acceptsExplicitNoUpdateWithoutArtifactFields() {
        val descriptor = ClickAppUpdateDescriptor.parse(
            JSONObject()
                .put("ok", true)
                .put("schema", ClickAppUpdateDescriptor.SCHEMA)
                .put("available", false),
            "https://click-mac.tailnet.ts.net",
        )
        assertFalse(descriptor.available)
    }

    @Test
    fun rejectsCrossOriginOversizedAndMalformedArtifacts() {
        fun payload() = JSONObject()
            .put("ok", true)
            .put("schema", ClickAppUpdateDescriptor.SCHEMA)
            .put("available", true)
            .put("version_code", 9)
            .put("version_name", "0.1.8")
            .put("apk_url", "/update.apk")
            .put("apk_bytes", 72_000_000)
            .put("apk_sha256", hash)
            .put("certificate_sha256", certificate)
            .put("min_sdk", 28)

        assertThrows(RuntimeException::class.java) {
            ClickAppUpdateDescriptor.parse(
                payload().put("apk_url", "https://evil.example/update.apk"),
                "https://click-mac.tailnet.ts.net",
            )
        }
        assertThrows(RuntimeException::class.java) {
            ClickAppUpdateDescriptor.parse(
                payload().put("apk_bytes", 300L * 1024L * 1024L),
                "https://click-mac.tailnet.ts.net",
            )
        }
        assertThrows(RuntimeException::class.java) {
            ClickAppUpdateDescriptor.parse(
                payload().put("apk_sha256", "not-a-hash"),
                "https://click-mac.tailnet.ts.net",
            )
        }
    }
}
