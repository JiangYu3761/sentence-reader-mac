package com.click.shell

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.nio.file.Files

class VerifiedAssetFileTest {
    @Test
    fun rejectedDownloadDoesNotReplaceKnownGoodAsset() {
        val directory = Files.createTempDirectory("click-asset-reject").toFile()
        val target = directory.resolve("book.epub").apply { writeText("known-good") }
        val pending = directory.resolve("book.epub.part").apply { writeText("partial") }

        val failure = runCatching {
            VerifiedAssetFile.verify(pending, VerifiedAssetFile.sha256(target), target.length())
        }.exceptionOrNull()

        assertTrue(failure is IllegalStateException)
        assertEquals("known-good", target.readText())
        assertEquals("partial", pending.readText())
    }

    @Test
    fun verifiedDownloadReplacesAssetAndRemovesBackup() {
        val directory = Files.createTempDirectory("click-asset-replace").toFile()
        val target = directory.resolve("book.epub").apply { writeText("old") }
        val pending = directory.resolve("book.epub.part").apply { writeText("new-complete") }

        VerifiedAssetFile.verify(pending, VerifiedAssetFile.sha256(pending), pending.length())
        VerifiedAssetFile.replace(pending, target)

        assertEquals("new-complete", target.readText())
        assertFalse(pending.exists())
        assertFalse(VerifiedAssetFile.backup(target).exists())
    }

    @Test
    fun interruptedReplacementRestoresPreviousAsset() {
        val directory = Files.createTempDirectory("click-asset-recover").toFile()
        val target = directory.resolve("book.epub")
        VerifiedAssetFile.backup(target).writeText("previous-good")

        VerifiedAssetFile.recover(target)

        assertEquals("previous-good", target.readText())
        assertFalse(VerifiedAssetFile.backup(target).exists())
    }
}
