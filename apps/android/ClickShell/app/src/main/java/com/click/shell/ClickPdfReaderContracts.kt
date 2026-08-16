package com.click.shell

import java.io.File

data class ClickPdfReaderInput(
    val bookId: String,
    val source: File,
)

data class ClickPdfInputValidation(
    val input: ClickPdfReaderInput? = null,
    val error: String = "",
)

data class ClickPdfRectSnapshot(
    val pageIndex: Int,
    val left: Float,
    val top: Float,
    val right: Float,
    val bottom: Float,
)

data class ClickPdfViewportPage(
    val pageIndex: Int,
    val left: Float,
    val top: Float,
    val right: Float,
    val bottom: Float,
)

data class ClickPdfTextSelectionSnapshot(
    val text: String,
    val bounds: List<ClickPdfRectSnapshot>,
) {
    val firstPageIndex: Int
        get() = bounds.minOfOrNull(ClickPdfRectSnapshot::pageIndex) ?: 0
}

enum class ClickPdfSelectionAction {
    RED_HIGHLIGHT,
    TEXT_NOTE,
    SPEAK_SENTENCE,
}

/**
 * Pure policy shared by the PDF Activity and its unit tests.
 *
 * The Activity deliberately accepts only a verified original already inside Click's filesDir.
 * A content URI, public path, cache file, symlink escape, or non-PDF is rejected here.
 */
internal object ClickPdfReaderPolicy {
    private const val MAX_BOOK_ID_LENGTH = 512
    private val APPROVED_SOURCE_ROOT_NAMES = listOf(
        "book-import-originals",
        "click-source-library",
    )
    private val PDF_MAGIC = byteArrayOf(
        '%'.code.toByte(),
        'P'.code.toByte(),
        'D'.code.toByte(),
        'F'.code.toByte(),
        '-'.code.toByte(),
    )

    fun validate(filesRoot: File, sourcePath: String?, bookId: String?): ClickPdfInputValidation {
        val cleanBookId = bookId.orEmpty().trim()
        if (!validBookId(cleanBookId)) {
            return ClickPdfInputValidation(error = "缺少有效的书籍编号")
        }
        if (sourcePath.isNullOrBlank()) {
            return ClickPdfInputValidation(error = "缺少手机私有 PDF")
        }

        return try {
            val root = filesRoot.canonicalFile
            val source = File(sourcePath).canonicalFile
            val filesPrefix = root.path.trimEnd(File.separatorChar) + File.separator
            val approvedSource = APPROVED_SOURCE_ROOT_NAMES.any { rootName ->
                val approvedRoot = File(root, rootName).canonicalFile
                val approvedPrefix =
                    approvedRoot.path.trimEnd(File.separatorChar) + File.separator
                approvedRoot.path.startsWith(filesPrefix) &&
                    source.path.startsWith(approvedPrefix)
            }
            when {
                !approvedSource ->
                    ClickPdfInputValidation(error = "只允许打开 Click 批准原件目录中的 PDF")
                !source.isFile ->
                    ClickPdfInputValidation(error = "PDF 本地副本不存在")
                !source.name.endsWith(".pdf", ignoreCase = true) ->
                    ClickPdfInputValidation(error = "本地副本不是 PDF")
                !hasPdfMagic(source) ->
                    ClickPdfInputValidation(error = "PDF 文件校验失败")
                else ->
                    ClickPdfInputValidation(ClickPdfReaderInput(cleanBookId, source))
            }
        } catch (_: Throwable) {
            ClickPdfInputValidation(error = "无法验证 PDF 本地副本")
        }
    }

    fun restoredPage(pageIndex: Int?, pageCount: Int): Int {
        if (pageCount <= 0) return 0
        return (pageIndex ?: 0).coerceIn(0, pageCount - 1)
    }

    fun pageRatio(pageIndex: Int, pageCount: Int): Double {
        if (pageCount <= 0) return 0.0
        return pageIndex.coerceIn(0, pageCount - 1).toDouble() / pageCount.toDouble()
    }

    fun pageIndexFromUserInput(value: String, pageCount: Int): Int? {
        if (pageCount <= 0) return null
        val pageNumber = value.trim().toIntOrNull() ?: return null
        return (pageNumber - 1).takeIf { it in 0 until pageCount }
    }

    fun primaryVisiblePage(
        firstVisiblePage: Int,
        viewportWidth: Int,
        viewportHeight: Int,
        pages: List<ClickPdfViewportPage>,
    ): Int {
        if (viewportWidth <= 0 || viewportHeight <= 0 || pages.isEmpty()) {
            return firstVisiblePage.coerceAtLeast(0)
        }
        val visibleAreas = pages
            .asSequence()
            .map { page ->
                val visibleWidth =
                    minOf(page.right, viewportWidth.toFloat()) - maxOf(page.left, 0f)
                val visibleHeight =
                    minOf(page.bottom, viewportHeight.toFloat()) - maxOf(page.top, 0f)
                page to (visibleWidth.coerceAtLeast(0f) * visibleHeight.coerceAtLeast(0f))
            }
            .filter { (_, visibleArea) -> visibleArea > 0f }
            .toList()
        val viewportArea = viewportWidth.toFloat() * viewportHeight.toFloat()

        // Keep the top page current while it still occupies a meaningful part of
        // the reading viewport. A partly visible next page must not take over the
        // label and saved position before the reader has actually moved to it.
        visibleAreas
            .filter { (_, visibleArea) -> visibleArea * 5f >= viewportArea }
            .minWithOrNull(
                compareBy<Pair<ClickPdfViewportPage, Float>> {
                    maxOf(it.first.top, 0f)
                }.thenBy { it.first.pageIndex },
            )
            ?.let { return it.first.pageIndex.coerceAtLeast(0) }

        return visibleAreas
            .maxWithOrNull(
                compareBy<Pair<ClickPdfViewportPage, Float>> { it.second }
                    .thenBy { -it.first.pageIndex },
            )
            ?.first
            ?.pageIndex
            ?.coerceAtLeast(0)
            ?: firstVisiblePage.coerceAtLeast(0)
    }

    fun validBookId(value: String): Boolean =
        value.isNotBlank() &&
            value.length <= MAX_BOOK_ID_LENGTH &&
            value.none { it.isISOControl() || it == '/' || it == '\\' }

    internal fun hasPdfMagic(source: File): Boolean {
        if (!source.isFile || source.length() < PDF_MAGIC.size) return false
        return source.inputStream().use { input ->
            val actual = ByteArray(PDF_MAGIC.size)
            input.read(actual) == PDF_MAGIC.size && actual.contentEquals(PDF_MAGIC)
        }
    }
}
