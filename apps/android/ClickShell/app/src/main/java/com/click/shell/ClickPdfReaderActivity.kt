package com.click.shell

import android.content.Context
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.View
import android.view.ViewGroup
import android.view.inputmethod.InputMethodManager
import android.widget.ArrayAdapter
import android.widget.EditText
import android.widget.ListView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.fragment.app.commitNow
import org.json.JSONArray
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

/**
 * Independent, read-only PDF reader shell.
 * It revalidates both launch extras and never trusts an external or shared-entry path.
 */
class ClickPdfReaderActivity :
    AppCompatActivity(),
    ClickPdfViewerFragment.Host {

    private val mainHandler = Handler(Looper.getMainLooper())
    private val io: ExecutorService = Executors.newSingleThreadExecutor()
    private val destroyed = AtomicBoolean(false)

    private lateinit var input: ClickPdfReaderInput
    private lateinit var store: ClickNativeStore
    private lateinit var preferences: ReaderPreferences
    private lateinit var viewer: ClickPdfViewerFragment
    private lateinit var readerRoot: View
    private lateinit var pageText: TextView
    private lateinit var selectionText: TextView
    private lateinit var searchAction: TextView

    private var documentReady = false
    private var searchActive = false
    private var restoringPosition = true
    private var pageCount = 1
    private var visiblePage = 0
    private var lastSavedPage = -1
    private var pendingPage = -1
    private var outlineLoading = false
    private var outlineResult: ClickPdfOutlineResult? = null

    private val persistPositionRunnable = Runnable {
        val page = pendingPage
        pendingPage = -1
        if (page >= 0) persistPosition(page)
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        val darkTheme = getSharedPreferences("click_reader_preferences_v1", Context.MODE_PRIVATE)
            .getString("theme", ReaderTheme.NIGHT.name) != ReaderTheme.DAY.name
        setTheme(if (darkTheme) R.style.ClickPdfReaderTheme else R.style.ClickPdfReaderLightTheme)
        super.onCreate(savedInstanceState)

        val validation = ClickPdfReaderPolicy.validate(
            filesRoot = filesDir,
            sourcePath = intent.getStringExtra(EXTRA_PRIVATE_PDF_PATH),
            bookId = intent.getStringExtra(EXTRA_BOOK_ID),
        )
        val validated = validation.input
        if (validated == null) {
            Toast.makeText(this, validation.error, Toast.LENGTH_LONG).show()
            finish()
            return
        }
        input = validated
        store = ClickNativeStore(applicationContext)
        preferences = ReaderPreferencesRepository(this).bindBook(input.bookId).load()
        ClickUi.applySystemBars(this, preferences.theme == ReaderTheme.NIGHT)

        setContentView(R.layout.activity_click_pdf_reader)
        readerRoot = findViewById(R.id.click_pdf_root)
        ViewCompat.setOnApplyWindowInsetsListener(readerRoot) { view, insets ->
            val systemBars = insets.getInsets(WindowInsetsCompat.Type.systemBars())
            view.setPadding(systemBars.left, systemBars.top, systemBars.right, systemBars.bottom)
            insets
        }
        ViewCompat.requestApplyInsets(readerRoot)
        pageText = findViewById(R.id.click_pdf_page)
        selectionText = findViewById(R.id.click_pdf_selection_status)
        searchAction = findViewById(R.id.click_pdf_search)

        findViewById<View>(R.id.click_pdf_back).setOnClickListener { finish() }
        findViewById<TextView>(R.id.click_pdf_title).text =
            intent.getStringExtra(EXTRA_BOOK_TITLE).orEmpty().trim()
                .ifBlank { input.source.nameWithoutExtension }
        findViewById<View>(R.id.click_pdf_outline).setOnClickListener { showOutlineOrPageJump() }
        searchAction.setOnClickListener { toggleSearch() }

        viewer = (supportFragmentManager.findFragmentByTag(PDF_FRAGMENT_TAG)
            as? ClickPdfViewerFragment)
            ?: ClickPdfViewerFragment.newInstance(input.source.absolutePath).also { fragment ->
                supportFragmentManager.commitNow {
                    replace(R.id.click_pdf_fragment_container, fragment, PDF_FRAGMENT_TAG)
                }
            }
    }

    override fun onPdfDocumentReady(pageCount: Int) {
        documentReady = true
        this.pageCount = pageCount.coerceAtLeast(1)
        selectionText.text = getString(R.string.click_pdf_selection_hint)
        loadPositionAndSidecarHighlights()
    }

    override fun onPdfDocumentError(message: String) {
        documentReady = false
        selectionText.text = getString(R.string.click_pdf_load_failed)
        Toast.makeText(this, message, Toast.LENGTH_LONG).show()
    }

    override fun onPdfViewportChanged(firstVisiblePage: Int) {
        visiblePage = ClickPdfReaderPolicy.restoredPage(firstVisiblePage, pageCount)
        updatePageLabel()
        if (restoringPosition || visiblePage == lastSavedPage) return
        pendingPage = visiblePage
        mainHandler.removeCallbacks(persistPositionRunnable)
        mainHandler.postDelayed(persistPositionRunnable, POSITION_DEBOUNCE_MILLIS)
    }

    override fun onPdfTextSelectionChanged(selection: ClickPdfTextSelectionSnapshot?) {
        selectionText.text = when {
            selection == null -> getString(R.string.click_pdf_selection_hint)
            selection.text.length > MAX_SELECTION_CHARACTERS ->
                getString(R.string.click_pdf_selection_too_long)
            else -> selection.text.replace(Regex("\\s+"), " ").take(SELECTION_PREVIEW_CHARACTERS)
        }
    }

    override fun onPdfSelectionAction(
        action: ClickPdfSelectionAction,
        selection: ClickPdfTextSelectionSnapshot?,
    ) {
        val selected = selection?.takeIf {
            it.text.isNotBlank() &&
                it.text.length <= MAX_SELECTION_CHARACTERS &&
                it.bounds.isNotEmpty() &&
                it.bounds.size <= MAX_SELECTION_BOUNDS
        }
        if (selected == null) {
            toast(getString(R.string.click_pdf_select_text_first))
            return
        }
        when (action) {
            ClickPdfSelectionAction.RED_HIGHLIGHT ->
                saveSidecarAnnotation(
                    operationType = "red_highlight_created",
                    kind = "red_highlight",
                    note = "",
                    color = "red",
                    selection = selected,
                )
            ClickPdfSelectionAction.TEXT_NOTE -> showNoteDialog(selected)
            ClickPdfSelectionAction.SPEAK_SENTENCE -> speakSelection(selected)
        }
    }

    override fun onStop() {
        mainHandler.removeCallbacks(persistPositionRunnable)
        if (::store.isInitialized && documentReady && !restoringPosition) {
            persistPosition(visiblePage)
        }
        super.onStop()
    }

    override fun onDestroy() {
        destroyed.set(true)
        mainHandler.removeCallbacksAndMessages(null)
        if (::store.isInitialized) {
            io.execute { store.close() }
        }
        io.shutdown()
        super.onDestroy()
    }

    private fun toggleSearch() {
        if (!documentReady) {
            toast(getString(R.string.click_pdf_wait_for_load))
            return
        }
        val requested = !searchActive
        if (viewer.setSearchActive(requested)) {
            searchActive = requested
            searchAction.text = getString(
                if (searchActive) R.string.click_pdf_search_close else R.string.click_pdf_search,
            )
        }
    }

    private fun loadPositionAndSidecarHighlights() {
        io.execute {
            val restored = runCatching {
                val raw = store.positionLocatorJson(input.bookId)
                val locator = raw.takeIf(String::isNotBlank)?.let(::JSONObject)
                locator
                    ?.takeIf { it.optString("mode") == PDF_POSITION_MODE }
                    ?.optInt("page_index")
            }.getOrNull()
            val highlightBounds = loadSidecarHighlightBounds()
            if (destroyed.get()) return@execute
            runOnUiThread {
                val restoredPage = ClickPdfReaderPolicy.restoredPage(restored, pageCount)
                visiblePage = restoredPage
                lastSavedPage = restoredPage
                updatePageLabel()
                viewer.showSidecarHighlights(highlightBounds)
                viewer.scrollToPage(restoredPage)
                mainHandler.postDelayed(
                    {
                        restoringPosition = false
                        if (visiblePage != lastSavedPage) {
                            pendingPage = visiblePage
                            mainHandler.post(persistPositionRunnable)
                        }
                    },
                    POSITION_RESTORE_SETTLE_MILLIS,
                )
            }
        }
    }

    private fun loadSidecarHighlightBounds(): List<ClickPdfRectSnapshot> =
        runCatching {
            store.annotations(input.bookId, "red_highlight").flatMap { row ->
                val locator = JSONObject(row.rangeLocatorJson)
                if (locator.optString("mode") != PDF_SELECTION_MODE) {
                    return@flatMap emptyList()
                }
                val bounds = locator.optJSONArray("bounds") ?: return@flatMap emptyList()
                (0 until bounds.length()).mapNotNull { index ->
                    bounds.optJSONObject(index)?.let { area ->
                        ClickPdfRectSnapshot(
                            pageIndex = area.optInt("page_index").coerceAtLeast(0),
                            left = area.optDouble("left").toFloat(),
                            top = area.optDouble("top").toFloat(),
                            right = area.optDouble("right").toFloat(),
                            bottom = area.optDouble("bottom").toFloat(),
                        ).takeIf { it.right > it.left && it.bottom > it.top }
                    }
                }
            }
        }.getOrDefault(emptyList())

    private fun persistPosition(page: Int) {
        if (!::store.isInitialized || !documentReady) return
        val safePage = ClickPdfReaderPolicy.restoredPage(page, pageCount)
        if (safePage == lastSavedPage) return
        lastSavedPage = safePage
        val total = pageCount
        io.execute {
            runCatching {
                val locator = JSONObject()
                    .put("mode", PDF_POSITION_MODE)
                    .put("source_kind", "pdf")
                    .put("page_index", safePage)
                    .put("total_pages", total)
                val payload = JSONObject()
                    .put("book_id", input.bookId)
                    .put("chapter_locator", "pdf-page:${safePage + 1}")
                    .put("page_index", safePage)
                    .put("total_pages", total)
                    .put("page_ratio", ClickPdfReaderPolicy.pageRatio(safePage, total))
                    .put("locator", locator)
                store.saveLocalPositionAndEnqueueLatest(
                    "android-pdf-position-${UUID.randomUUID()}",
                    input.bookId,
                    payload,
                )
                ClickSyncScheduler.enqueueMetadata(applicationContext)
            }.onFailure {
                if (!destroyed.get()) runOnUiThread { toast(getString(R.string.click_pdf_position_failed)) }
            }
        }
    }

    private fun showNoteDialog(selection: ClickPdfTextSelectionSnapshot) {
        val inputField = EditText(this).apply {
            minLines = 3
            maxLines = 8
            hint = getString(R.string.click_pdf_note_hint)
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.click_pdf_note_title)
            .setMessage(selection.text.take(NOTE_SELECTION_PREVIEW_CHARACTERS))
            .setView(inputField)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.click_pdf_save) { _, _ ->
                saveSidecarAnnotation(
                    operationType = "note_created",
                    kind = "note",
                    note = inputField.text.toString().trim(),
                    color = "",
                    selection = selection,
                )
            }
            .show()
    }

    private fun saveSidecarAnnotation(
        operationType: String,
        kind: String,
        note: String,
        color: String,
        selection: ClickPdfTextSelectionSnapshot,
    ) {
        io.execute {
            val outcome = runCatching {
                val rangeLocator = selectionRangeLocator(selection)
                val metadata = JSONObject()
                    .put("source", "android_pdf")
                    .put("renderer", PDF_RENDERER_CONTRACT)
                    .put("pdf_writeback", false)
                val payload = JSONObject()
                    .put("book_id", input.bookId)
                    .put("kind", kind)
                    .put("source_text", selection.text)
                    .put("note_text", note)
                    .put("color", color)
                    .put("chapter_locator", "pdf-page:${selection.firstPageIndex + 1}")
                    .put("range_locator", rangeLocator)
                    .put("metadata", metadata)
                val operationId = "android-pdf-annotation-${UUID.randomUUID()}"
                store.saveLocalAnnotationAndEnqueue(
                    operationId,
                    operationType,
                    input.bookId,
                    payload,
                )
                ClickSyncScheduler.enqueueMetadata(applicationContext)
                store.pendingCount()
            }
            if (destroyed.get()) return@execute
            runOnUiThread {
                outcome.onSuccess { pending ->
                    viewer.clearTextSelection()
                    if (kind == "red_highlight") refreshSidecarHighlights()
                    toast(getString(R.string.click_pdf_saved_pending, pending))
                }.onFailure {
                    toast(getString(R.string.click_pdf_annotation_failed))
                }
            }
        }
    }

    private fun selectionRangeLocator(selection: ClickPdfTextSelectionSnapshot): JSONObject {
        val bounds = JSONArray()
        selection.bounds.forEach { area ->
            bounds.put(
                JSONObject()
                    .put("page_index", area.pageIndex)
                    .put("left", area.left.toDouble())
                    .put("top", area.top.toDouble())
                    .put("right", area.right.toDouble())
                    .put("bottom", area.bottom.toDouble()),
            )
        }
        return JSONObject()
            .put("mode", PDF_SELECTION_MODE)
            .put("source_kind", "pdf")
            .put("page_index", selection.firstPageIndex)
            .put("bounds", bounds)
    }

    private fun refreshSidecarHighlights() {
        io.execute {
            val highlights = loadSidecarHighlightBounds()
            if (!destroyed.get()) runOnUiThread { viewer.showSidecarHighlights(highlights) }
        }
    }

    private fun speakSelection(selection: ClickPdfTextSelectionSnapshot) {
        val sync = ClickSyncConfig.load(this)
        val locator = JSONObject()
            .put("mode", PDF_POSITION_MODE)
            .put("page_index", selection.firstPageIndex)
            .put("total_pages", pageCount)
            .toString()
        ReaderTtsSession.start(
            context = this,
            sourceSegments = ReaderTtsTextNormalizer.segment(selection.text),
            prefs = preferences,
            network = ReaderTtsNetworkConfig(
                baseUrl = sync.baseUrl,
                deviceId = sync.deviceId,
                accessToken = sync.accessToken,
                bookId = input.bookId,
                locator = locator,
            ),
        )
        viewer.clearTextSelection()
    }

    private fun updatePageLabel() {
        pageText.text = getString(R.string.click_pdf_page_count, visiblePage + 1, pageCount)
    }

    private fun showOutlineOrPageJump() {
        if (!documentReady) {
            toast(getString(R.string.click_pdf_wait_for_load))
            return
        }
        outlineResult?.let(::showOutlineResult)
        if (outlineResult != null || outlineLoading) return
        outlineLoading = true
        toast(getString(R.string.click_pdf_outline_loading))
        io.execute {
            val result = ClickPdfOutlineReader(applicationContext).read(input.source, pageCount)
            if (destroyed.get()) return@execute
            runOnUiThread {
                outlineLoading = false
                outlineResult = result
                showOutlineResult(result)
            }
        }
    }

    private fun showOutlineResult(result: ClickPdfOutlineResult) {
        if (result.entries.isEmpty()) {
            AlertDialog.Builder(this)
                .setTitle(R.string.click_pdf_outline_title)
                .setMessage(result.message.ifBlank { getString(R.string.click_pdf_outline_empty) })
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.click_pdf_page_jump_action) { _, _ ->
                    showPageJumpDialog()
                }
                .show()
            return
        }
        val list = ListView(this)
        val dialog = AlertDialog.Builder(this)
            .setTitle(
                if (result.truncated) {
                    getString(R.string.click_pdf_outline_title_truncated, result.entries.size)
                } else {
                    getString(R.string.click_pdf_outline_title)
                },
            )
            .setView(list)
            .setNeutralButton(R.string.click_pdf_page_jump_action) { _, _ -> showPageJumpDialog() }
            .setNegativeButton(android.R.string.cancel, null)
            .create()
        list.adapter = object : ArrayAdapter<ClickPdfOutlineEntry>(
            this,
            android.R.layout.simple_list_item_1,
            result.entries,
        ) {
            override fun isEnabled(position: Int): Boolean =
                getItem(position)?.pageIndex != null

            override fun getView(position: Int, convertView: View?, parent: ViewGroup): View {
                val view = super.getView(position, convertView, parent) as TextView
                val entry = getItem(position) ?: return view
                view.text = entry.pageIndex?.let { "${entry.title}  ·  ${it + 1}" } ?: entry.title
                view.isEnabled = entry.pageIndex != null
                view.setPadding(
                    dp(14 + entry.depth.coerceAtMost(8) * 18),
                    dp(4),
                    dp(10),
                    dp(4),
                )
                return view
            }
        }
        list.setOnItemClickListener { _, _, position, _ ->
            val entry = result.entries[position]
            val target = entry.pageIndex ?: return@setOnItemClickListener
            // PdfView is the source of truth: short documents can already fit the
            // whole viewport and therefore cannot physically scroll to the target.
            viewer.scrollToPage(target)
            dialog.dismiss()
        }
        dialog.show()
    }

    private fun showPageJumpDialog() {
        if (!documentReady) {
            toast(getString(R.string.click_pdf_wait_for_load))
            return
        }
        val inputField = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            isSingleLine = true
            hint = getString(R.string.click_pdf_page_jump_hint, pageCount)
            setText((visiblePage + 1).toString())
            selectAll()
        }
        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.click_pdf_page_jump_title)
            .setMessage(R.string.click_pdf_page_jump_boundary)
            .setView(inputField)
            .setNegativeButton(android.R.string.cancel, null)
            .setPositiveButton(R.string.click_pdf_page_jump_action, null)
            .create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val target = ClickPdfReaderPolicy.pageIndexFromUserInput(
                    inputField.text.toString(),
                    pageCount,
                )
                if (target == null) {
                    inputField.error = getString(R.string.click_pdf_page_jump_invalid, pageCount)
                } else {
                    dismissImeThenScroll(inputField, dialog, target)
                }
            }
        }
        dialog.show()
    }

    private fun dismissImeThenScroll(inputField: EditText, dialog: AlertDialog, target: Int) {
        (getSystemService(Context.INPUT_METHOD_SERVICE) as? InputMethodManager)
            ?.hideSoftInputFromWindow(inputField.windowToken, 0)
        dialog.dismiss()
        scrollWhenImeHidden(target, PAGE_JUMP_IME_POLL_ATTEMPTS)
    }

    private fun scrollWhenImeHidden(target: Int, attemptsRemaining: Int) {
        readerRoot.postDelayed(
            {
                if (destroyed.get()) return@postDelayed
                val imeVisible =
                    ViewCompat.getRootWindowInsets(readerRoot)
                        ?.isVisible(WindowInsetsCompat.Type.ime()) == true
                if (imeVisible && attemptsRemaining > 0) {
                    scrollWhenImeHidden(target, attemptsRemaining - 1)
                    return@postDelayed
                }
                readerRoot.postOnAnimation {
                    if (destroyed.get()) return@postOnAnimation
                    // Update the label only from onPdfViewportChanged after the
                    // platform viewer confirms that the viewport actually moved.
                    viewer.scrollToPage(target)
                }
            },
            PAGE_JUMP_IME_POLL_MILLIS,
        )
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt().coerceAtLeast(1)

    private fun toast(message: String) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
    }

    companion object {
        const val EXTRA_PRIVATE_PDF_PATH = "private_pdf_path"
        const val EXTRA_BOOK_ID = "book_id"
        const val EXTRA_BOOK_TITLE = "book_title"

        private const val PDF_FRAGMENT_TAG = "click_pdf_viewer"
        private const val PDF_POSITION_MODE = "androidx_pdf_position_v1"
        private const val PDF_SELECTION_MODE = "androidx_pdf_text_selection_v1"
        private const val PDF_RENDERER_CONTRACT = "androidx.pdf:pdf-viewer-fragment:1.0.0-alpha19"
        private const val POSITION_DEBOUNCE_MILLIS = 650L
        private const val POSITION_RESTORE_SETTLE_MILLIS = 650L
        private const val PAGE_JUMP_IME_POLL_MILLIS = 50L
        private const val PAGE_JUMP_IME_POLL_ATTEMPTS = 40
        private const val MAX_SELECTION_CHARACTERS = 20_000
        private const val MAX_SELECTION_BOUNDS = 2_048
        private const val SELECTION_PREVIEW_CHARACTERS = 120
        private const val NOTE_SELECTION_PREVIEW_CHARACTERS = 400
    }
}
