package com.click.shell

import android.content.Context
import android.text.Html

/**
 * Builds a non-visual speech lookahead from the durable EPUB spine. It never turns the visible
 * Readium page merely to obtain future text.
 */
class ReaderTtsBookLookaheadProvider(
    context: Context,
    private val bookId: String,
    private val initialChapterLocator: String,
) : ReaderTtsLookaheadProvider {
    private val appContext = context.applicationContext

    override fun load(
        currentText: String,
        estimatedTargetMs: Long,
        ratePercent: Int,
    ): ReaderTtsLookaheadPlan {
        if (bookId.isBlank()) return ReaderTtsLookaheadPlan(emptyList(), true)
        ClickNativeStore(appContext).use { store ->
            val chapters = store.chapters(bookId)
            if (chapters.isEmpty()) return ReaderTtsLookaheadPlan(emptyList(), true)
            val locatorIndex = chapters.indexOfFirst { sameLocator(it.locator, initialChapterLocator) }
                .takeIf { it >= 0 }
                ?: 0
            val normalizedCurrent = ReaderTtsTextNormalizer.normalize(currentText)
            var startChapter = locatorIndex
            var startSegment = 0
            var currentMatched = normalizedCurrent.isBlank()
            if (normalizedCurrent.isNotBlank()) {
                outer@ for (chapterPosition in locatorIndex until chapters.size) {
                    val chapter = chapters[chapterPosition]
                    val segments = chapterSegments(store.chapterHtml(bookId, chapter.index))
                    val match = segments.indexOfFirst { candidate ->
                        val normalized = ReaderTtsTextNormalizer.normalize(candidate)
                        normalized == normalizedCurrent ||
                            normalized.contains(normalizedCurrent) ||
                            normalizedCurrent.contains(normalized)
                    }
                    if (match >= 0) {
                        startChapter = chapterPosition
                        startSegment = match
                        currentMatched = true
                        break@outer
                    }
                }
            }
            if (!currentMatched) return ReaderTtsLookaheadPlan(emptyList(), false)

            val target = estimatedTargetMs.coerceAtLeast(ReaderTtsBufferPolicy.HIGH_WATER_MS)
            val result = mutableListOf<ReaderTtsLookaheadSegment>()
            var estimated = 0L
            var reachedEnd = true
            for (chapterPosition in startChapter until chapters.size) {
                val chapter = chapters[chapterPosition]
                val segments = chapterSegments(store.chapterHtml(bookId, chapter.index))
                val from = if (chapterPosition == startChapter) startSegment.coerceAtMost(segments.size) else 0
                for (segmentPosition in from until segments.size) {
                    val text = segments[segmentPosition]
                    result += ReaderTtsLookaheadSegment(text, chapter.locator)
                    estimated += ReaderTtsBufferPolicy.estimatedDurationMs(text, ratePercent)
                    if (estimated >= target || result.size >= MAX_SEGMENTS) {
                        reachedEnd = false
                        break
                    }
                }
                if (!reachedEnd) break
            }
            return ReaderTtsLookaheadPlan(result, reachedEnd)
        }
    }

    private fun chapterSegments(html: String): List<String> {
        val cleaned = html
            .replace(Regex("(?is)<script\\b.*?</script>"), " ")
            .replace(Regex("(?is)<style\\b.*?</style>"), " ")
        val plain = Html.fromHtml(cleaned, Html.FROM_HTML_MODE_LEGACY).toString()
        return ReaderTtsTextNormalizer.segment(plain)
    }

    private fun sameLocator(left: String, right: String): Boolean {
        val a = normalizeLocator(left)
        val b = normalizeLocator(right)
        return a.isNotBlank() && b.isNotBlank() &&
            (a == b || a.endsWith("/$b") || b.endsWith("/$a"))
    }

    private fun normalizeLocator(value: String): String =
        value.substringBefore('#').substringBefore('?').replace('\\', '/').trimStart('/').trim()

    companion object {
        private const val MAX_SEGMENTS = 4_000
    }
}
