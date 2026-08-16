package com.click.shell

import java.io.File
import java.nio.file.Files
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TingleCaptureRepositoryPolicyTest {
    @Test
    fun captureIdsCannotEscapeThePrivateRoot() {
        assertTrue(TingleCaptureRepository.isSafeCaptureId("android-safe_01.capture"))
        assertFalse(TingleCaptureRepository.isSafeCaptureId("../escape"))
        assertFalse(TingleCaptureRepository.isSafeCaptureId("nested/capture"))
        assertFalse(TingleCaptureRepository.isSafeCaptureId(""))
    }

    @Test
    fun verificationCodeIsStableAndDerivedFromTheCaptureId() {
        assertEquals(
            "2E6C157196C9",
            TingleCaptureRepository.verificationCode("android-safe_01.capture"),
        )
    }

    @Test
    fun aSyncedCaptureIsNeverSelectedForUploadAgain() {
        val audio = File.createTempFile("tingle-policy-", ".wav")
        try {
            audio.writeBytes(ByteArray(45))
            val pending = capture(audio, TingleCaptureRepository.SYNC_PENDING, true)
            val synced = capture(audio, TingleCaptureRepository.SYNC_SYNCED, false)

            assertTrue(pending.isEligibleForUpload())
            assertFalse(synced.isEligibleForUpload())
        } finally {
            audio.delete()
        }
    }

    @Test
    fun authenticationFailureCanBeRetriedButPermanentFailureCannot() {
        val audio = File.createTempFile("tingle-policy-", ".wav")
        try {
            audio.writeBytes(ByteArray(45))
            val authRequired = capture(
                audio,
                TingleCaptureRepository.SYNC_FAILED,
                true,
            )
            val permanent = capture(
                audio,
                TingleCaptureRepository.SYNC_FAILED,
                false,
            )

            assertTrue(authRequired.isEligibleForUpload())
            assertFalse(permanent.isEligibleForUpload())
        } finally {
            audio.delete()
        }
    }

    @Test
    fun retryableOfflineFailureRemainsPendingInTheUserInterface() {
        val audio = File.createTempFile("tingle-status-", ".wav")
        try {
            audio.writeBytes(ByteArray(45))
            val retryable = capture(
                audio,
                TingleCaptureRepository.SYNC_FAILED,
                true,
            )
            val permanent = capture(
                audio,
                TingleCaptureRepository.SYNC_FAILED,
                false,
            )

            assertEquals("待同步", TingleLocalActivity.statusText(retryable, true))
            assertEquals("同步失败", TingleLocalActivity.statusText(permanent, true))
        } finally {
            audio.delete()
        }
    }

    @Test
    fun interruptedWavHeaderIsRepairedWithoutDeletingTheOriginal() {
        val audio = File.createTempFile("tingle-interrupted-", ".wav")
        try {
            audio.writeBytes(ByteArray(76))
            TingleCaptureRepository.repairWavHeader(audio)
            val bytes = audio.readBytes()
            assertTrue(audio.isFile)
            assertTrue(bytes.size == 76)
            assertTrue(littleEndianInt(bytes, 4) == 68)
            assertTrue(littleEndianInt(bytes, 40) == 32)
        } finally {
            audio.delete()
        }
    }

    @Test
    fun platformParentAliasDoesNotMakeThePrivateCaptureRootASymlink() {
        val tree = Files.createTempDirectory("tingle-parent-alias-").toFile()
        try {
            val realFiles = File(tree, "real-files").apply { mkdirs() }
            val aliasedFiles = File(tree, "aliased-files")
            Files.createSymbolicLink(aliasedFiles.toPath(), realFiles.toPath())
            val root = File(aliasedFiles, TingleCaptureRepository.ROOT_DIRECTORY_NAME)
                .apply { mkdirs() }
            val captureDirectory = File(root, "android-safe-capture").apply { mkdir() }

            assertTrue(TingleCaptureRepository.isSafeRootDirectory(root))
            assertTrue(TingleCaptureRepository.isSafeDirectChild(root, captureDirectory))
        } finally {
            tree.deleteRecursively()
        }
    }

    @Test
    fun captureRootItselfCannotBeASymbolicLink() {
        val tree = Files.createTempDirectory("tingle-root-symlink-").toFile()
        try {
            val realRoot = File(tree, "real-root").apply { mkdirs() }
            val linkedRoot = File(tree, TingleCaptureRepository.ROOT_DIRECTORY_NAME)
            Files.createSymbolicLink(linkedRoot.toPath(), realRoot.toPath())

            assertFalse(TingleCaptureRepository.isSafeRootDirectory(linkedRoot))
        } finally {
            tree.deleteRecursively()
        }
    }

    @Test
    fun captureDirectoryItselfCannotBeASymbolicLink() {
        val tree = Files.createTempDirectory("tingle-capture-symlink-").toFile()
        try {
            val root = File(tree, TingleCaptureRepository.ROOT_DIRECTORY_NAME).apply { mkdirs() }
            val realCapture = File(tree, "real-capture").apply { mkdirs() }
            val linkedCapture = File(root, "android-linked-capture")
            Files.createSymbolicLink(linkedCapture.toPath(), realCapture.toPath())

            assertFalse(TingleCaptureRepository.isSafeDirectChild(root, linkedCapture))
        } finally {
            tree.deleteRecursively()
        }
    }

    private fun littleEndianInt(bytes: ByteArray, offset: Int): Int =
        (bytes[offset].toInt() and 0xff) or
            ((bytes[offset + 1].toInt() and 0xff) shl 8) or
            ((bytes[offset + 2].toInt() and 0xff) shl 16) or
            ((bytes[offset + 3].toInt() and 0xff) shl 24)

    private fun capture(
        audio: File,
        syncState: String,
        retryable: Boolean,
    ) = TingleCaptureRepository.Capture(
        "android-policy-capture",
        audio,
        "device",
        "",
        "",
        if (syncState == TingleCaptureRepository.SYNC_SYNCED) "synced" else "pending_upload",
        syncState,
        if (syncState == TingleCaptureRepository.SYNC_SYNCED) "rec_existing" else "",
        "hash",
        1.0,
        1,
        0,
        retryable,
        "",
        "",
        "",
        0L,
    )
}
