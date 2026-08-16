package com.click.shell

import org.junit.Assert.assertEquals
import org.junit.Test

class TingleUploaderPolicyTest {
    @Test
    fun oneWorkerOwnsAtMostOneCaptureAndJsonSuffixClosesTheAudioString() {
        assertEquals(1, TingleUploader.MAX_CAPTURES_PER_RUN)
        assertEquals(
            "\",\"mime_type\":\"audio/wav\"}",
            TingleUploader.HttpTransport.metadataSuffix("{\"mime_type\":\"audio/wav\"}"),
        )
    }

    @Test
    fun authenticationAndTransientStatusesDoNotBecomePermanentRetries() {
        assertEquals(
            TingleUploader.FailureKind.AUTH_REQUIRED,
            TingleUploader.classifyHttpStatus(401),
        )
        assertEquals(
            TingleUploader.FailureKind.AUTH_REQUIRED,
            TingleUploader.classifyHttpStatus(403),
        )
        assertEquals(
            TingleUploader.FailureKind.RETRYABLE,
            TingleUploader.classifyHttpStatus(408),
        )
        assertEquals(
            TingleUploader.FailureKind.RETRYABLE,
            TingleUploader.classifyHttpStatus(429),
        )
        assertEquals(
            TingleUploader.FailureKind.RETRYABLE,
            TingleUploader.classifyHttpStatus(503),
        )
        assertEquals(
            TingleUploader.FailureKind.PERMANENT,
            TingleUploader.classifyHttpStatus(409),
        )
        assertEquals(
            true,
            TingleUploader.canRetryAfterExternalEvent(
                TingleUploader.FailureKind.AUTH_REQUIRED,
            ),
        )
        assertEquals(
            false,
            TingleUploader.canRetryAfterExternalEvent(
                TingleUploader.FailureKind.PERMANENT,
            ),
        )
    }
}
