package com.click.shell

import android.content.Context
import android.graphics.Color
import android.graphics.RectF
import android.net.Uri
import android.os.Bundle
import android.util.SparseArray
import android.view.View
import androidx.pdf.ExperimentalPdfApi
import androidx.pdf.Highlight
import androidx.pdf.PdfDocument
import androidx.pdf.PdfRect
import androidx.pdf.selection.ContextMenuComponent
import androidx.pdf.selection.Selection
import androidx.pdf.selection.SelectionMenuComponent
import androidx.pdf.selection.model.TextSelection
import androidx.pdf.view.PdfView
import androidx.pdf.viewer.fragment.PdfViewerFragment
import java.io.File

/**
 * Thin alpha19 adapter. It owns no library, import, or sync decisions and never writes into a PDF.
 */
class ClickPdfViewerFragment : PdfViewerFragment() {
    interface Host {
        fun onPdfDocumentReady(pageCount: Int)
        fun onPdfDocumentError(message: String)
        fun onPdfViewportChanged(firstVisiblePage: Int)
        fun onPdfTextSelectionChanged(selection: ClickPdfTextSelectionSnapshot?)
        fun onPdfSelectionAction(
            action: ClickPdfSelectionAction,
            selection: ClickPdfTextSelectionSnapshot?,
        )
    }

    private var host: Host? = null
    private var clickPdfView: PdfView? = null
    private var currentTextSelection: ClickPdfTextSelectionSnapshot? = null
    private var firstContentReady = false
    private var pendingPageIndex: Int? = null
    private var pendingHighlights: List<ClickPdfRectSnapshot> = emptyList()
    private var documentPageCount = 0

    private val selectionChangedListener = object : PdfView.OnSelectionChangedListener {
        override fun onSelectionChanged(newSelection: Selection?) {
            currentTextSelection = (newSelection as? TextSelection)?.let { selected ->
                // alpha19 supplies the text order. Do not reconstruct it from rectangles: ordering
                // across multi-column or cross-page selections remains a physical-device acceptance.
                ClickPdfTextSelectionSnapshot(
                    text = selected.text.toString().trim(),
                    bounds = selected.bounds.map { area ->
                        ClickPdfRectSnapshot(
                            pageIndex = area.pageNum,
                            left = area.left,
                            top = area.top,
                            right = area.right,
                            bottom = area.bottom,
                        )
                    },
                ).takeIf { it.text.isNotBlank() && it.bounds.isNotEmpty() }
            }
            host?.onPdfTextSelectionChanged(currentTextSelection)
        }
    }

    private val viewportChangedListener = object : PdfView.OnViewportChangedListener {
        override fun onViewportChanged(
            firstVisiblePage: Int,
            visiblePagesCount: Int,
            pageLocations: SparseArray<RectF>,
            zoomLevel: Float,
        ) {
            val view = clickPdfView
            val pages = (0 until pageLocations.size()).map { index ->
                val location = pageLocations.valueAt(index)
                ClickPdfViewportPage(
                    pageIndex = pageLocations.keyAt(index),
                    left = location.left,
                    top = location.top,
                    right = location.right,
                    bottom = location.bottom,
                )
            }
            host?.onPdfViewportChanged(
                ClickPdfReaderPolicy.primaryVisiblePage(
                    firstVisiblePage = firstVisiblePage,
                    viewportWidth = view?.width ?: 0,
                    viewportHeight = view?.height ?: 0,
                    pages = pages,
                ),
            )
        }
    }

    private val firstContentLoadListener = PdfView.OnFirstContentLoadListener {
        firstContentReady = true
        pendingPageIndex?.let { page ->
            clickPdfView?.scrollToPage(safePage(page))
            pendingPageIndex = null
        }
        applyPendingHighlights()
    }

    private val menuItemPreparer = object : PdfView.SelectionMenuItemPreparer {
        override fun onPrepareSelectionMenuItems(
            components: MutableList<ContextMenuComponent>,
        ) {
            components += selectionMenuItem(
                key = "click_pdf_red_highlight",
                label = "标红",
                description = "把所选 PDF 文字保存到 Click 标红",
                action = ClickPdfSelectionAction.RED_HIGHLIGHT,
            )
            components += selectionMenuItem(
                key = "click_pdf_text_note",
                label = "备注",
                description = "给所选 PDF 文字添加 Click 备注",
                action = ClickPdfSelectionAction.TEXT_NOTE,
            )
            components += selectionMenuItem(
                key = "click_pdf_speak_sentence",
                label = "朗读本句",
                description = "使用 Click 当前朗读设置播放所选文字",
                action = ClickPdfSelectionAction.SPEAK_SENTENCE,
            )
        }
    }

    override fun onAttach(context: Context) {
        super.onAttach(context)
        host = context as? Host
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        isToolboxVisible = false
        if (documentUri == null) {
            val path = requireArguments().getString(ARG_PRIVATE_PDF_PATH).orEmpty()
            documentUri = Uri.fromFile(File(path))
        }
    }

    @OptIn(ExperimentalPdfApi::class)
    override fun onPdfViewCreated(pdfView: PdfView) {
        super.onPdfViewCreated(pdfView)
        this.clickPdfView = pdfView
        firstContentReady = false
        pdfView.isFormFillingEnabled = false
        pdfView.addOnFirstContentLoadListener(firstContentLoadListener)
        pdfView.addOnSelectionChangedListener(selectionChangedListener)
        pdfView.addOnViewportChangedListener(viewportChangedListener)
        pdfView.addSelectionMenuItemPreparer(menuItemPreparer)
    }

    override fun onLoadDocumentSuccess(document: PdfDocument) {
        super.onLoadDocumentSuccess(document)
        isToolboxVisible = false
        documentPageCount = document.pageCount.coerceAtLeast(1)
        host?.onPdfDocumentReady(documentPageCount)
    }

    override fun onLoadDocumentError(error: Throwable) {
        super.onLoadDocumentError(error)
        host?.onPdfDocumentError(error.message.orEmpty().ifBlank { "PDF 无法打开" })
    }

    override fun onRequestImmersiveMode(enterImmersive: Boolean) {
        super.onRequestImmersiveMode(enterImmersive)
        // Click keeps this viewer read-only; the alpha19 annotation/form toolbox must stay hidden.
        isToolboxVisible = false
    }

    override fun onDestroyView() {
        clickPdfView?.removeOnFirstContentLoadListener(firstContentLoadListener)
        clickPdfView?.removeOnSelectionChangedListener(selectionChangedListener)
        clickPdfView?.removeOnViewportChangedListener(viewportChangedListener)
        clickPdfView?.removeSelectionMenuItemPreparer(menuItemPreparer)
        clickPdfView = null
        firstContentReady = false
        pendingPageIndex = null
        pendingHighlights = emptyList()
        documentPageCount = 0
        currentTextSelection = null
        super.onDestroyView()
    }

    override fun onDetach() {
        host = null
        super.onDetach()
    }

    fun setSearchActive(active: Boolean): Boolean {
        if (documentUri == null) return false
        isTextSearchActive = active
        return isTextSearchActive == active
    }

    fun scrollToPage(pageIndex: Int) {
        val requestedPage = safePage(pageIndex)
        pendingPageIndex = requestedPage
        if (firstContentReady) {
            clickPdfView?.scrollToPage(requestedPage)
            pendingPageIndex = null
        }
    }

    fun clearTextSelection() {
        clickPdfView?.clearCurrentSelection()
        currentTextSelection = null
    }

    fun showSidecarHighlights(bounds: List<ClickPdfRectSnapshot>) {
        pendingHighlights = bounds
        if (firstContentReady) applyPendingHighlights()
    }

    private fun applyPendingHighlights() {
        val color = Color.argb(145, 239, 82, 82)
        clickPdfView?.setHighlights(
            pendingHighlights
                .filter { area ->
                    area.pageIndex in 0 until documentPageCount &&
                        area.left.isFinite() &&
                        area.top.isFinite() &&
                        area.right.isFinite() &&
                        area.bottom.isFinite() &&
                        area.right > area.left &&
                        area.bottom > area.top
                }
                .map { area ->
                Highlight(
                    PdfRect(
                        area.pageIndex,
                        area.left,
                        area.top,
                        area.right,
                        area.bottom,
                    ),
                    color,
                )
            },
        )
    }

    private fun safePage(pageIndex: Int): Int =
        if (documentPageCount > 0) {
            pageIndex.coerceIn(0, documentPageCount - 1)
        } else {
            pageIndex.coerceAtLeast(0)
        }

    private fun selectionMenuItem(
        key: String,
        label: String,
        description: String,
        action: ClickPdfSelectionAction,
    ): SelectionMenuComponent =
        SelectionMenuComponent(
            key = key,
            label = label,
            contentDescription = description,
        ) {
            close()
            host?.onPdfSelectionAction(action, currentTextSelection)
        }

    companion object {
        private const val ARG_PRIVATE_PDF_PATH = "private_pdf_path"

        fun newInstance(privatePdfPath: String): ClickPdfViewerFragment =
            ClickPdfViewerFragment().apply {
                arguments = Bundle().apply {
                    putString(ARG_PRIVATE_PDF_PATH, privatePdfPath)
                }
            }
    }
}
