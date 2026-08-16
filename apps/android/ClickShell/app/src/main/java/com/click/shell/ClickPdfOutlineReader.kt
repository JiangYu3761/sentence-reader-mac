package com.click.shell

import android.content.Context
import com.tom_roush.pdfbox.android.PDFBoxResourceLoader
import com.tom_roush.pdfbox.cos.COSDictionary
import com.tom_roush.pdfbox.pdmodel.encryption.InvalidPasswordException
import com.tom_roush.pdfbox.io.MemoryUsageSetting
import com.tom_roush.pdfbox.pdmodel.PDDocument
import com.tom_roush.pdfbox.pdmodel.interactive.documentnavigation.outline.PDOutlineNode
import java.io.File
import java.util.IdentityHashMap

data class ClickPdfOutlineEntry(
    val title: String,
    val pageIndex: Int?,
    val depth: Int,
)

data class ClickPdfOutlineResult(
    val entries: List<ClickPdfOutlineEntry> = emptyList(),
    val message: String = "",
    val truncated: Boolean = false,
)

/**
 * A narrow, on-demand catalog-outline reader. AndroidX PDF stays the renderer.
 *
 * This deliberately rejects encrypted PDFs and caps traversal. It never extracts page text,
 * writes the source, or keeps a PDDocument alive after the explicit outline request returns.
 */
class ClickPdfOutlineReader(context: Context) {
    private val appContext = context.applicationContext
    private val scratch = File(appContext.cacheDir, "click-pdf-outline").apply { mkdirs() }

    fun read(source: File, expectedPages: Int): ClickPdfOutlineResult {
        if (!source.isFile) return ClickPdfOutlineResult(message = "PDF 本地副本不存在")
        PDFBoxResourceLoader.init(appContext)
        val runScratch = File(scratch, "outline-${System.nanoTime()}").apply { mkdirs() }
        return try {
            val memory = MemoryUsageSetting.setupMixed(MAX_MAIN_MEMORY_BYTES)
                .setTempDir(runScratch)
            PDDocument.load(source, memory).use { document ->
                if (document.isEncrypted) {
                    return@use ClickPdfOutlineResult(
                        message = "加密 PDF 暂不读取目录，可按页码跳转",
                    )
                }
                val outline = document.documentCatalog.documentOutline
                    ?: return@use ClickPdfOutlineResult(message = "这份 PDF 没有文档目录，可按页码跳转")
                val pageLimit = minOf(document.numberOfPages, expectedPages.coerceAtLeast(1))
                val pageIndexes = IdentityHashMap<COSDictionary, Int>(pageLimit)
                for ((index, page) in document.pages.withIndex()) {
                    if (index >= pageLimit) break
                    pageIndexes[page.cosObject] = index
                }
                val entries = mutableListOf<ClickPdfOutlineEntry>()
                val truncated = collect(
                    document = document,
                    node = outline,
                    depth = 0,
                    pageLimit = pageLimit,
                    pageIndexes = pageIndexes,
                    destination = entries,
                )
                if (entries.none { it.pageIndex != null }) {
                    ClickPdfOutlineResult(message = "这份 PDF 没有可导航的文档目录，可按页码跳转")
                } else {
                    ClickPdfOutlineResult(entries = entries, truncated = truncated)
                }
            }
        } catch (_: InvalidPasswordException) {
            ClickPdfOutlineResult(message = "加密 PDF 暂不读取目录，可按页码跳转")
        } catch (_: Throwable) {
            ClickPdfOutlineResult(message = "目录解析失败，可继续按页码跳转")
        } finally {
            runScratch.deleteRecursively()
        }
    }

    private fun collect(
        document: PDDocument,
        node: PDOutlineNode,
        depth: Int,
        pageLimit: Int,
        pageIndexes: IdentityHashMap<COSDictionary, Int>,
        destination: MutableList<ClickPdfOutlineEntry>,
    ): Boolean {
        if (depth > MAX_DEPTH || destination.size >= MAX_ENTRIES) return true
        for (item in node.children()) {
            if (destination.size >= MAX_ENTRIES) return true
            val page = runCatching { item.findDestinationPage(document) }.getOrNull()
            val pageIndex = page?.cosObject?.let(pageIndexes::get) ?: -1
            val title = item.title.orEmpty().replace(Regex("\\s+"), " ").trim()
            if (title.isNotEmpty()) {
                destination += ClickPdfOutlineEntry(
                    title = title.take(MAX_TITLE_CHARACTERS),
                    pageIndex = pageIndex.takeIf { it in 0 until pageLimit },
                    depth = depth,
                )
            }
            if (item.hasChildren()) {
                if (collect(document, item, depth + 1, pageLimit, pageIndexes, destination)) return true
            }
        }
        return false
    }

    companion object {
        private const val MAX_MAIN_MEMORY_BYTES = 8L * 1024L * 1024L
        private const val MAX_ENTRIES = 2_000
        private const val MAX_DEPTH = 16
        private const val MAX_TITLE_CHARACTERS = 500
    }
}
