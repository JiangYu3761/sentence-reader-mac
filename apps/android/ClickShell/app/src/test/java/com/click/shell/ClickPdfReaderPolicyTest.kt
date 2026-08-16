package com.click.shell

import java.io.File
import java.nio.file.Files
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ClickPdfReaderPolicyTest {
    @Test
    fun acceptsVerifiedPdfInsideAppFiles() {
        withTemporaryTree { tree ->
            val filesRoot = File(tree, "files").apply { mkdirs() }
            listOf("book-import-originals", "click-source-library").forEach { approvedRoot ->
                val pdf = File(filesRoot, "$approvedRoot/books/example.PDF").apply {
                    parentFile?.mkdirs()
                    writeBytes("%PDF-1.7\n".toByteArray())
                }

                val result = ClickPdfReaderPolicy.validate(filesRoot, pdf.path, "book-42")

                assertNotNull(result.input)
                assertEquals("book-42", result.input?.bookId)
                assertEquals(pdf.canonicalFile, result.input?.source)
                assertTrue(result.error.isEmpty())
            }
        }
    }

    @Test
    fun rejectsFilesOutsideApprovedSourceRoots() {
        withTemporaryTree { tree ->
            val filesRoot = File(tree, "files").apply { mkdirs() }
            val unapprovedPrivatePdf = File(filesRoot, "other/private.pdf").apply {
                parentFile?.mkdirs()
                writeBytes("%PDF-1.7\n".toByteArray())
            }

            val result =
                ClickPdfReaderPolicy.validate(filesRoot, unapprovedPrivatePdf.path, "book-42")

            assertNull(result.input)
            assertEquals("只允许打开 Click 批准原件目录中的 PDF", result.error)
        }
    }

    @Test
    fun rejectsSymlinkEscapingApprovedSourceRoot() {
        withTemporaryTree { tree ->
            val filesRoot = File(tree, "files").apply { mkdirs() }
            val approvedRoot = File(filesRoot, "book-import-originals").apply { mkdirs() }
            val outside = File(tree, "outside.pdf").apply {
                writeBytes("%PDF-1.7\n".toByteArray())
            }
            val linkedPdf = File(approvedRoot, "linked.pdf")
            Files.createSymbolicLink(linkedPdf.toPath(), outside.toPath())

            val result = ClickPdfReaderPolicy.validate(filesRoot, linkedPdf.path, "book-42")

            assertNull(result.input)
            assertEquals("只允许打开 Click 批准原件目录中的 PDF", result.error)
        }
    }

    @Test
    fun rejectsApprovedRootThatIsItselfASymlink() {
        withTemporaryTree { tree ->
            val filesRoot = File(tree, "files").apply { mkdirs() }
            val outsideRoot = File(tree, "outside-root").apply { mkdirs() }
            val outsidePdf = File(outsideRoot, "book.pdf").apply {
                writeBytes("%PDF-1.7\n".toByteArray())
            }
            val linkedRoot = File(filesRoot, "click-source-library")
            Files.createSymbolicLink(linkedRoot.toPath(), outsideRoot.toPath())

            val result =
                ClickPdfReaderPolicy.validate(
                    filesRoot,
                    File(linkedRoot, outsidePdf.name).path,
                    "book-42",
                )

            assertNull(result.input)
            assertEquals("只允许打开 Click 批准原件目录中的 PDF", result.error)
        }
    }

    @Test
    fun rejectsWrongExtensionAndWrongHeader() {
        withTemporaryTree { tree ->
            val filesRoot = File(tree, "files").apply { mkdirs() }
            val approvedRoot = File(filesRoot, "click-source-library").apply { mkdirs() }
            val wrongExtension = File(approvedRoot, "book.txt").apply {
                writeBytes("%PDF-1.7\n".toByteArray())
            }
            val wrongHeader = File(approvedRoot, "book.pdf").apply {
                writeText("not a PDF")
            }

            assertEquals(
                "本地副本不是 PDF",
                ClickPdfReaderPolicy.validate(filesRoot, wrongExtension.path, "book-42").error,
            )
            assertEquals(
                "PDF 文件校验失败",
                ClickPdfReaderPolicy.validate(filesRoot, wrongHeader.path, "book-42").error,
            )
        }
    }

    @Test
    fun clampsPagesAndRejectsUnsafeBookIds() {
        assertEquals(0, ClickPdfReaderPolicy.restoredPage(null, 12))
        assertEquals(0, ClickPdfReaderPolicy.restoredPage(-4, 12))
        assertEquals(11, ClickPdfReaderPolicy.restoredPage(99, 12))
        assertEquals(0.0, ClickPdfReaderPolicy.pageRatio(0, 12), 0.0)
        assertEquals(11.0 / 12.0, ClickPdfReaderPolicy.pageRatio(11, 12), 0.0)
        assertTrue(ClickPdfReaderPolicy.validBookId("book-42"))
        assertFalse(ClickPdfReaderPolicy.validBookId("../book"))
        assertFalse(ClickPdfReaderPolicy.validBookId("book\n42"))
    }

    @Test
    fun pageJumpAcceptsOnlyOneBasedPagesInsideDocument() {
        assertEquals(0, ClickPdfReaderPolicy.pageIndexFromUserInput("1", 12))
        assertEquals(11, ClickPdfReaderPolicy.pageIndexFromUserInput(" 12 ", 12))
        assertNull(ClickPdfReaderPolicy.pageIndexFromUserInput("0", 12))
        assertNull(ClickPdfReaderPolicy.pageIndexFromUserInput("13", 12))
        assertNull(ClickPdfReaderPolicy.pageIndexFromUserInput("目录", 12))
        assertNull(ClickPdfReaderPolicy.pageIndexFromUserInput("1", 0))
    }

    @Test
    fun primaryVisiblePagePrefersThePageOccupyingMostOfTheViewport() {
        val pages = listOf(
            ClickPdfViewportPage(15, 0f, -1_300f, 1_000f, 80f),
            ClickPdfViewportPage(16, 0f, 100f, 1_000f, 1_900f),
        )

        assertEquals(
            16,
            ClickPdfReaderPolicy.primaryVisiblePage(
                firstVisiblePage = 15,
                viewportWidth = 1_000,
                viewportHeight = 2_000,
                pages = pages,
            ),
        )
    }

    @Test
    fun primaryVisiblePageFallsBackWhenLocationsAreUnavailable() {
        assertEquals(
            4,
            ClickPdfReaderPolicy.primaryVisiblePage(
                firstVisiblePage = 4,
                viewportWidth = 0,
                viewportHeight = 2_000,
                pages = emptyList(),
            ),
        )
    }

    @Test
    fun primaryVisiblePageKeepsTopPageWhileItStillOccupiesReadingViewport() {
        val pages = listOf(
            ClickPdfViewportPage(0, 123f, 328f, 957f, 1_408f),
            ClickPdfViewportPage(1, 0f, 1_437f, 1_080f, 2_271f),
        )

        assertEquals(
            0,
            ClickPdfReaderPolicy.primaryVisiblePage(
                firstVisiblePage = 0,
                viewportWidth = 1_080,
                viewportHeight = 1_948,
                pages = pages,
            ),
        )
    }

    private fun withTemporaryTree(block: (File) -> Unit) {
        val tree = Files.createTempDirectory("click-pdf-policy").toFile()
        try {
            block(tree)
        } finally {
            tree.deleteRecursively()
        }
    }
}
