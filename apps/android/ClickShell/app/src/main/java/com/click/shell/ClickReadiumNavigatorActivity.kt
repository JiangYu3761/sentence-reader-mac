@file:OptIn(org.readium.r2.shared.ExperimentalReadiumApi::class)

package com.click.shell

import android.Manifest
import android.app.AlertDialog
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.res.Configuration
import android.content.res.ColorStateList
import android.graphics.Color
import android.graphics.PointF
import android.graphics.drawable.GradientDrawable
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.text.Editable
import android.text.TextWatcher
import android.content.pm.PackageManager
import android.view.ActionMode
import android.view.Gravity
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.SeekBar
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.core.view.ViewCompat
import androidx.core.view.WindowInsetsCompat
import androidx.fragment.app.FragmentActivity
import androidx.fragment.app.commit
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.flow.collectLatest
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import org.readium.r2.navigator.Decoration
import org.readium.r2.navigator.HyperlinkNavigator
import org.readium.r2.navigator.epub.EpubNavigatorFragment
import org.readium.r2.navigator.epub.EpubNavigatorFactory
import org.readium.r2.navigator.input.DragEvent
import org.readium.r2.navigator.input.InputListener
import org.readium.r2.navigator.input.TapEvent
import org.readium.r2.shared.publication.Link
import org.readium.r2.shared.publication.Locator
import org.readium.r2.shared.util.AbsoluteUrl
import org.readium.r2.shared.util.Url
import org.readium.r2.shared.util.asset.Asset
import org.readium.r2.shared.util.asset.AssetRetriever
import org.readium.r2.shared.util.data.ReadError
import org.readium.r2.shared.util.http.DefaultHttpClient
import org.readium.r2.shared.util.mediatype.MediaType
import org.readium.r2.streamer.PublicationOpener
import org.readium.r2.streamer.parser.DefaultPublicationParser
import java.io.File
import java.io.FileOutputStream
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.util.UUID
import kotlin.math.abs

class ClickReadiumNavigatorActivity :
    FragmentActivity(),
    EpubNavigatorFragment.Listener,
    EpubNavigatorFragment.PaginationListener {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Main)
    private val mainHandler = Handler(Looper.getMainLooper())
    private val gestureGate = ReaderGestureGate()
    private lateinit var store: ClickNativeStore
    private lateinit var audioNoteRepository: ReaderAudioNoteRepository
    private lateinit var preferencesRepository: ReaderPreferencesRepository
    private lateinit var readerPreferences: ReaderPreferences
    private lateinit var localTtsModelManager: LocalTtsModelManager
    private lateinit var fontManager: ReaderFontManager
    private lateinit var root: FrameLayout
    private lateinit var publicationFrame: FrameLayout
    private lateinit var topChrome: LinearLayout
    private lateinit var bottomChrome: LinearLayout
    private lateinit var progressText: TextView
    private lateinit var readerTitle: TextView
    private lateinit var ttsMiniPlayer: LinearLayout
    private lateinit var ttsMiniSentence: TextView
    private lateinit var ttsMiniPlayPause: Button
    private lateinit var ttsMiniRate: Button
    private lateinit var ttsMiniTimer: Button
    private lateinit var ttsMiniSecondaryControls: LinearLayout
    private var containerId: Int = View.generateViewId()
    private var navigator: EpubNavigatorFragment? = null
    private var publication: org.readium.r2.shared.publication.Publication? = null
    private var asset: Asset? = null

    private var baseUrl: String = ""
    private var deviceId: String = ""
    private var accessToken: String = ""
    private var bookId: String = ""
    private var epubPath: String = ""
    private var readingProfile: String = "UNKNOWN"
    private var displayVariantsPath: String = ""
    private var displayVariantPayload: JSONObject = JSONObject()
    private val expandedTocHrefs = mutableSetOf<String>()
    private var showTocOnLaunch = false
    private var pendingAudioLocatorJson: String = ""
    private var pendingVoiceNoteLocatorJson: String = ""
    private var activeRecorder: MediaRecorder? = null
    private var activeAudioFile: File? = null
    private var activeNoteRecognizer: SpeechRecognizer? = null
    private var activeNoteDialog: AlertDialog? = null
    private var activeTtsSettingsDialog: AlertDialog? = null
    private var activeNoteListening = false
    private var activeVoicePlayer: MediaPlayer? = null
    private var activeVoicePlaybackFile: File? = null
    private var activeVoicePlaybackTemporary = false
    private var activeVoiceNoteId: String = ""
    private var lastLocatorJson: JSONObject = JSONObject()
    private var ttsTimerMinutes: Int = 60
    private var chromeVisible = false
    private var selectionActionModeActive = false
    private var selectionActionMode: ActionMode? = null
    private var lastPageNumber = 1
    private var lastPageTotal = 1
    private var lastTtsHighlightText = ""
    private var pendingTtsResumeLocatorJson = ""
    private var ttsMiniExpanded = false
    private var systemLeftInset = 0
    private var systemRightInset = 0
    private var systemBottomInset = 0
    private var preparedViewportAnchor: Locator? = null
    private var viewportPreviewAnchor: Locator? = null
    private var pendingViewportAnchorRestore: Runnable? = null
    private var renderedTtsMiniSnapshot: ReaderTtsSnapshot? = null

    private val ttsContinuationProvider = ReaderTtsContinuationProvider { direction, onReady ->
        if (direction > 0) requestNextTtsPage(onReady) else requestPreviousTtsPage(onReady)
    }

    private val ttsListener = ReaderTtsSession.Listener { state ->
        runOnUiThread {
            if (::progressText.isInitialized && state.state != ReaderTtsState.IDLE) {
                progressText.text = when (state.state) {
                    ReaderTtsState.STOPPED, ReaderTtsState.ERROR -> "$lastPageNumber/$lastPageTotal"
                    else -> when {
                        state.total > 0 -> "${state.engineLabel} · ${state.index + 1}/${state.total} · ${state.message}"
                        else -> "${state.engineLabel} · ${state.message}"
                    }
                }
            }
            updateTtsTextHighlight(state)
            renderTtsMiniPlayer(state)
        }
    }

    private val selectionActionModeCallback = object : ActionMode.Callback {
        override fun onCreateActionMode(mode: ActionMode, menu: Menu): Boolean {
            selectionActionModeActive = true
            selectionActionMode = mode
            menu.clear()
            selectionActions.forEachIndexed { index, label ->
                menu.add(Menu.NONE, SELECTION_MENU_BASE + index, index, label)
                    .setShowAsAction(
                        if (index < PRIMARY_SELECTION_ACTION_COUNT) {
                            MenuItem.SHOW_AS_ACTION_IF_ROOM
                        } else {
                            MenuItem.SHOW_AS_ACTION_NEVER
                        },
                    )
            }
            return true
        }

        override fun onPrepareActionMode(mode: ActionMode, menu: Menu): Boolean = false

        override fun onActionItemClicked(mode: ActionMode, item: MenuItem): Boolean {
            val index = item.itemId - SELECTION_MENU_BASE
            if (index !in selectionActions.indices) return false
            // Capture the locator before closing ActionMode. Several WebView versions collapse
            // the selection as soon as the system menu finishes.
            performSelectionAction(index) { mode.finish() }
            return true
        }

        override fun onDestroyActionMode(mode: ActionMode) {
            selectionActionModeActive = false
            if (selectionActionMode === mode) selectionActionMode = null
        }
    }

    private val readerInputListener = object : InputListener {
        override fun onTap(event: TapEvent): Boolean {
            handleReaderTap(event.point)
            return true
        }

        // Readium is the sole owner of drag paging. Returning false preserves native text
        // selection and guarantees Click never adds a second page turn to the same swipe.
        override fun onDrag(event: DragEvent): Boolean = false
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        store = ClickNativeStore(this)
        audioNoteRepository = ReaderAudioNoteRepository(this)
        baseUrl = cleanBaseUrl(intent.getStringExtra(EXTRA_BASE_URL).orEmpty())
        deviceId = intent.getStringExtra(EXTRA_DEVICE_ID).orEmpty()
        accessToken = intent.getStringExtra(EXTRA_ACCESS_TOKEN).orEmpty().ifBlank(::storedAccessToken)
        bookId = intent.getStringExtra(EXTRA_BOOK_ID).orEmpty()
        epubPath = intent.getStringExtra(EXTRA_EPUB_PATH).orEmpty()
        pendingTtsResumeLocatorJson = intent.getStringExtra(EXTRA_TTS_RESUME_LOCATOR).orEmpty()
            .ifBlank { intent.getStringExtra(EXTRA_TARGET_LOCATOR).orEmpty() }
        readingProfile = intent.getStringExtra(EXTRA_READING_PROFILE).orEmpty()
            .ifBlank { store.bookReadingProfile(bookId).orEmpty() }
            .ifBlank { "UNKNOWN" }
        displayVariantsPath = intent.getStringExtra(EXTRA_DISPLAY_VARIANTS_PATH).orEmpty()
            .ifBlank { store.bookDisplayVariantsLocalPath(bookId).orEmpty() }
        displayVariantPayload = runCatching {
            File(displayVariantsPath).takeIf(File::isFile)?.readText(Charsets.UTF_8)?.let(::JSONObject) ?: JSONObject()
        }.getOrDefault(JSONObject())
        preferencesRepository = ReaderPreferencesRepository(this).bindBook(bookId)
        readerPreferences = preferencesRepository.load()
        localTtsModelManager = LocalTtsModelManager(this)
        fontManager = ReaderFontManager(this)
        ttsTimerMinutes = readerPreferences.ttsTimerMinutes
        showTocOnLaunch = intent.getBooleanExtra(EXTRA_SHOW_TOC, false)

        root = FrameLayout(this).apply {
            setBackgroundColor(readerBackgroundColor())
        }
        ClickUi.applySystemBars(this, readerPreferences.theme == ReaderTheme.NIGHT)
        setContentView(root)
        renderChrome("Readium 正在打开...")
        openReadiumPublication()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        val targetBookId = intent.getStringExtra(EXTRA_BOOK_ID).orEmpty()
        if (targetBookId.isNotBlank() && targetBookId != bookId) {
            startActivity(Intent(intent).apply { flags = 0 })
            return
        }
        setIntent(intent)
        val rawLocator = intent.getStringExtra(EXTRA_TTS_RESUME_LOCATOR).orEmpty()
            .ifBlank { intent.getStringExtra(EXTRA_TARGET_LOCATOR).orEmpty() }
        if (rawLocator.isBlank()) return
        pendingTtsResumeLocatorJson = rawLocator
        val openedPublication = publication ?: return
        val locator = restoredReadiumLocator(openedPublication) ?: return
        if (navigator?.go(locator, animated = false) == true) {
            pendingTtsResumeLocatorJson = ""
        }
    }

    override fun onStart() {
        super.onStart()
        ReaderTtsSession.addListener(ttsListener)
        ClickSyncScheduler.enqueueMetadata(this)
    }

    override fun onConfigurationChanged(newConfig: Configuration) {
        super.onConfigurationChanged(newConfig)
        if (readingProfile == "IMAGE_COMIC" || readingProfile == "FIXED_LAYOUT") {
            applyReaderPreferences(readerPreferences)
        }
    }

    override fun onStop() {
        ReaderTtsSession.removeListener(ttsListener)
        super.onStop()
    }

    override fun onDestroy() {
        ReaderTtsSession.clearContinuationProvider(ttsContinuationProvider)
        navigator?.removeInputListener(readerInputListener)
        mainHandler.removeCallbacksAndMessages(null)
        gestureGate.clear()
        releaseRecorder(deleteFile = true)
        releaseNoteRecognizer()
        releaseVoiceNotePlayer()
        activeTtsSettingsDialog?.dismiss()
        activeTtsSettingsDialog = null
        scope.cancel()
        publication?.close()
        publication = null
        asset?.close()
        asset = null
        store.close()
        super.onDestroy()
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (resultCode != RESULT_OK) return
        val uri = data?.data ?: return
        if (requestCode == REQUEST_READER_FONT) {
            scope.launch(Dispatchers.IO) {
                runCatching { fontManager.importFont(uri) }
                    .onSuccess { status ->
                        runOnUiThread {
                            applyReaderPreferences(readerPreferences)
                            applyImportedFontToCurrentPage()
                        }
                        toast("字体已导入 · 当前：${status.resolvedLabel}")
                    }
                    .onFailure { toast("字体导入失败：${it.compact()}") }
            }
            return
        }
        if (requestCode == REQUEST_LOCAL_TTS_MODEL) {
            scope.launch(Dispatchers.IO) {
                runCatching { localTtsModelManager.importSupportedPackage(uri) }
                    .onSuccess { result ->
                        val selected = result.voices.firstOrNull { it.voiceId == ReaderPreferences.DEFAULT_LOCAL_VOICE }
                            ?: result.voices.first()
                        readerPreferences = preferencesRepository.save(
                            readerPreferences.copy(
                                ttsLocalModelId = result.modelId,
                                ttsLocalVoiceId = selected.voiceId,
                                ttsMode = ReaderTtsMode.LOCAL_PRIVATE,
                            ),
                        )
                        toast("本地语音包已安装：${selected.displayName}")
                        runOnUiThread {
                            activeTtsSettingsDialog?.dismiss()
                            activeTtsSettingsDialog = null
                            showReadiumTtsSettingsDialog()
                        }
                    }
                    .onFailure { toast("语音包安装失败：${it.compact()}") }
            }
            return
        }
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == REQUEST_NOTIFICATION_PERMISSION) {
            if (grantResults.firstOrNull() != PackageManager.PERMISSION_GRANTED) {
                toast("未开启通知权限；朗读仍可继续，但切到后台后看不到通知栏控制")
            }
            return
        }
        if (requestCode != REQUEST_RECORD_AUDIO_PERMISSION) return
        if (grantResults.firstOrNull() != PackageManager.PERMISSION_GRANTED) {
            val voiceLocator = runCatching {
                Locator.fromJSON(JSONObject(pendingVoiceNoteLocatorJson))
            }.getOrNull()
            toast("没有麦克风权限，已改为键盘备注")
            pendingAudioLocatorJson = ""
            pendingVoiceNoteLocatorJson = ""
            if (voiceLocator != null) showKeyboardNoteDialog(voiceLocator)
            return
        }
        val voiceLocator = runCatching {
            Locator.fromJSON(JSONObject(pendingVoiceNoteLocatorJson))
        }.getOrNull()
        val audioLocator = runCatching {
            Locator.fromJSON(JSONObject(pendingAudioLocatorJson))
        }.getOrNull()
        pendingAudioLocatorJson = ""
        pendingVoiceNoteLocatorJson = ""
        when {
            voiceLocator != null -> showNoteDialog(voiceLocator)
            audioLocator != null -> showAudioRecorder(audioLocator)
        }
    }

    private fun renderChrome(status: String) {
        root.removeAllViews()
        root.setBackgroundColor(readerBackgroundColor())
        ClickUi.applySystemBars(this, readerPreferences.theme == ReaderTheme.NIGHT)

        publicationFrame = FrameLayout(this).apply {
            id = containerId
            setBackgroundColor(readerBackgroundColor())
        }
        root.addView(
            publicationFrame,
            FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT)
        )

        topChrome = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(dp(8), dp(4), dp(8), dp(4))
            background = rounded(chromeBackgroundColor(), dp(0), chromeBackgroundColor())
        }
        topChrome.addView(
            toolbarIconButton(R.drawable.ic_click_back, "返回书架") {
                finish()
                true
            },
        )
        readerTitle = TextView(this).apply {
                text = status.replace("Readium ", "")
                textSize = 16f
                setTypeface(android.graphics.Typeface.DEFAULT_BOLD)
                setTextColor(chromeTextColor())
                gravity = Gravity.CENTER_VERTICAL
                maxLines = 1
            }
        topChrome.addView(
            readerTitle,
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1f)
        )
        topChrome.addView(
            toolbarIconButton(R.drawable.ic_click_more, "更多阅读操作") {
                showReadiumMoreDialog()
                true
            },
        )
        root.addView(
            topChrome,
            FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48), Gravity.TOP)
        )

        bottomChrome = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = rounded(chromeBackgroundColor(), dp(0), chromeBackgroundColor())
        }
        progressText = TextView(this).apply {
                text = "正在定位"
                textSize = 13f
                setTextColor(chromeMutedColor())
                gravity = Gravity.CENTER
                visibility = if (readerPreferences.showProgress) View.VISIBLE else View.GONE
            }
        bottomChrome.addView(
            progressText,
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(20))
        )
        val actions = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        listOf(
            bottomIconButton(R.drawable.ic_click_toc, "目录") { showReadiumTocDialog(); true },
            bottomIconButton(R.drawable.ic_click_play, "朗读") { showReadiumTtsDialog(); true },
            bottomButton("Aa") { showReadiumVisualDialog(); true }.apply {
                contentDescription = "排版"
            },
        ).forEach { action ->
            actions.addView(action, LinearLayout.LayoutParams(0, dp(48), 1f))
        }
        bottomChrome.addView(actions, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48)))
        root.addView(
            bottomChrome,
            FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(68), Gravity.BOTTOM)
        )
        buildTtsMiniPlayer()
        ViewCompat.setOnApplyWindowInsetsListener(root) { _, insets ->
            val safeInsets = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() or WindowInsetsCompat.Type.displayCutout(),
            )
            systemLeftInset = safeInsets.left
            systemRightInset = safeInsets.right
            systemBottomInset = safeInsets.bottom
            updatePublicationViewportPadding()
            bottomChrome.setPadding(0, 0, 0, systemBottomInset)
            bottomChrome.layoutParams = (bottomChrome.layoutParams as FrameLayout.LayoutParams).apply {
                height = dp(68) + systemBottomInset
            }
            updateTtsMiniLayout()
            insets
        }
        ViewCompat.requestApplyInsets(root)
        setChromeVisible(false)
        renderTtsMiniPlayer(ReaderTtsSession.current())
    }

    private fun updatePublicationViewportPadding() {
        if (!::publicationFrame.isInitialized) return
        publicationFrame.setPadding(
            systemLeftInset + dp(readerPreferences.viewportLeftDp),
            dp(readerPreferences.viewportTopDp),
            systemRightInset + dp(readerPreferences.viewportRightDp),
            systemBottomInset + dp(readerPreferences.viewportBottomDp),
        )
    }

    private fun setChromeVisible(visible: Boolean) {
        chromeVisible = visible
        topChrome.visibility = if (visible) View.VISIBLE else View.GONE
        bottomChrome.visibility = if (visible) View.VISIBLE else View.GONE
        updateTtsMiniLayout()
    }

    private fun buildTtsMiniPlayer() {
        renderedTtsMiniSnapshot = null
        ttsMiniPlayer = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(10), dp(6), dp(10), dp(6))
            background = rounded(
                ClickUi.elevated(this@ClickReadiumNavigatorActivity, readerPreferences.theme == ReaderTheme.NIGHT),
                dp(20),
                ClickUi.separator(this@ClickReadiumNavigatorActivity, readerPreferences.theme == ReaderTheme.NIGHT),
            )
            elevation = dp(6).toFloat()
            visibility = View.GONE
        }
        ttsMiniSentence = TextView(this).apply {
            textSize = 14f
            setTextColor(chromeTextColor())
            gravity = Gravity.CENTER_VERTICAL
            minHeight = dp(42)
            maxLines = 1
            ellipsize = android.text.TextUtils.TruncateAt.END
            contentDescription = "当前朗读句子，点按展开或收起"
            setOnClickListener {
                ttsMiniExpanded = !ttsMiniExpanded
                updateTtsMiniLayout()
            }
        }
        ttsMiniPlayer.addView(
            ttsMiniSentence,
            LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
            ),
        )

        val controls = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        controls.addView(
            miniTtsIconButton(R.drawable.ic_click_previous, "上一句") { ReaderTtsSession.previous() },
            LinearLayout.LayoutParams(0, dp(38), 1f),
        )
        ttsMiniPlayPause = miniTtsIconButton(
            R.drawable.ic_click_pause,
            "暂停",
        ) { ReaderTtsSession.pauseOrResume() }
        controls.addView(ttsMiniPlayPause, LinearLayout.LayoutParams(0, dp(38), 1f))
        controls.addView(
            miniTtsIconButton(R.drawable.ic_click_next, "下一句") { ReaderTtsSession.next() },
            LinearLayout.LayoutParams(0, dp(38), 1f),
        )
        ttsMiniPlayer.addView(
            controls,
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(38)),
        )

        ttsMiniSecondaryControls = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
            visibility = View.GONE
        }
        ttsMiniRate = miniTtsButton("1.0×", "调整语速") { cycleTtsRate() }
        ttsMiniSecondaryControls.addView(ttsMiniRate, LinearLayout.LayoutParams(0, dp(38), 1f))
        ttsMiniTimer = miniTtsButton("⏱60", "重新开始倒计时") {
            applyTtsTimer(ReaderTtsSession.current().timerMinutes)
        }
        ttsMiniSecondaryControls.addView(ttsMiniTimer, LinearLayout.LayoutParams(0, dp(38), 1f))
        ttsMiniSecondaryControls.addView(
            miniTtsButton("×", "退出听音模式") { ReaderTtsSession.stop() },
            LinearLayout.LayoutParams(0, dp(38), 1f),
        )
        ttsMiniPlayer.addView(
            ttsMiniSecondaryControls,
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(38)),
        )
        root.addView(
            ttsMiniPlayer,
            FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.BOTTOM,
            ).apply {
                leftMargin = dp(8)
                rightMargin = dp(8)
            },
        )
        updateTtsMiniLayout()
    }

    private fun miniTtsButton(label: String, description: String, action: () -> Unit): Button =
        Button(this).apply {
            text = label
            contentDescription = description
            isAllCaps = false
            textSize = 12f
            setTextColor(chromeTextColor())
            minWidth = 0
            minimumWidth = 0
            minHeight = 0
            minimumHeight = 0
            setPadding(0, 0, 0, 0)
            background = ClickUi.ripple(
                ClickUi.surface(this@ClickReadiumNavigatorActivity, readerPreferences.theme == ReaderTheme.NIGHT),
                dp(14).toFloat(),
                ClickUi.separator(this@ClickReadiumNavigatorActivity, readerPreferences.theme == ReaderTheme.NIGHT),
                1,
                ClickUi.accent(this@ClickReadiumNavigatorActivity, readerPreferences.theme == ReaderTheme.NIGHT),
            )
            setOnClickListener { action() }
        }

    private fun miniTtsIconButton(
        iconResource: Int,
        description: String,
        action: () -> Unit,
    ): Button = miniTtsButton("", description, action).apply {
        ClickUi.styleIconButton(
            this,
            iconResource,
            description,
            readerPreferences.theme == ReaderTheme.NIGHT,
        )
    }

    private fun renderTtsMiniPlayer(state: ReaderTtsSnapshot) {
        if (!::ttsMiniPlayer.isInitialized || renderedTtsMiniSnapshot == state) return
        renderedTtsMiniSnapshot = state
        if (
            state.state == ReaderTtsState.IDLE ||
            state.state == ReaderTtsState.STOPPED ||
            state.state == ReaderTtsState.ERROR
        ) {
            ttsMiniPlayer.visibility = View.GONE
            ttsMiniExpanded = false
            updateTtsMiniLayout()
            return
        }
        ttsMiniSentence.text = state.currentText.ifBlank { state.message.ifBlank { "正在准备朗读" } }
        val paused = state.state == ReaderTtsState.PAUSED
        ClickUi.styleIconButton(
            ttsMiniPlayPause,
            if (paused) R.drawable.ic_click_play else R.drawable.ic_click_pause,
            if (paused) "继续播放" else "暂停",
            readerPreferences.theme == ReaderTheme.NIGHT,
        )
        ttsMiniRate.text = formatTtsRate(state.ratePercent)
        ttsMiniTimer.text = "⏱${state.timerMinutes}"
        ttsMiniPlayer.visibility = View.VISIBLE
        updateTtsMiniLayout()
    }

    private fun updateTtsMiniLayout() {
        if (!::ttsMiniPlayer.isInitialized) return
        ttsMiniSentence.maxLines = if (ttsMiniExpanded) 3 else 1
        ttsMiniSecondaryControls.visibility = if (ttsMiniExpanded) View.VISIBLE else View.GONE
        val miniActive = ttsMiniPlayer.visibility == View.VISIBLE
        bottomChrome.visibility = if (chromeVisible && !miniActive) View.VISIBLE else View.GONE
        val params = ttsMiniPlayer.layoutParams as? FrameLayout.LayoutParams ?: return
        params.height = ViewGroup.LayoutParams.WRAP_CONTENT
        params.bottomMargin = systemBottomInset + dp(8)
        ttsMiniPlayer.layoutParams = params
    }

    private fun formatTtsRate(ratePercent: Int): String =
        if (ratePercent % 100 == 0) "${ratePercent / 100}.0×" else "${ratePercent / 100.0}×"

    private fun cycleTtsRate() {
        val values = listOf(80, 90, 100, 110, 125, 150)
        val current = ReaderTtsSession.current().ratePercent
        val next = values.firstOrNull { it > current } ?: values.first()
        readerPreferences = preferencesRepository.save(readerPreferences.copy(ttsRatePercent = next))
        ReaderTtsSession.setRate(next)
    }

    private fun applyTtsTimer(minutes: Int) {
        val safe = minutes.takeIf { it == 30 || it == 60 || it == 90 } ?: 60
        ttsTimerMinutes = safe
        readerPreferences = preferencesRepository.save(readerPreferences.copy(ttsTimerMinutes = safe))
        val active = when (ReaderTtsSession.current().state) {
            ReaderTtsState.PREPARING,
            ReaderTtsState.PLAYING,
            ReaderTtsState.PAUSED,
            -> true
            else -> false
        }
        if (active) {
            ReaderTtsSession.setTimer(safe)
            toast("$safe 分钟后停止")
        } else {
            toast("已设为 $safe 分钟，开始朗读后计时")
        }
    }

    private fun handleReaderTap(point: PointF) {
        scope.launch {
            val selected = navigator?.currentSelection()?.locator?.let(::selectionText).orEmpty()
            if (selected.isNotBlank()) return@launch
            val width = publicationFrame.width.takeIf { it > 0 } ?: resources.displayMetrics.widthPixels
            when {
                chromeVisible -> setChromeVisible(false)
                point.x < width * 0.20f -> requestPageTurn(-1)
                point.x > width * 0.80f -> requestPageTurn(1)
                else -> setChromeVisible(true)
            }
        }
    }

    private fun requestPageTurn(direction: Int, gestureId: String = "tap-${System.nanoTime()}") {
        if (gestureGate.request(gestureId, direction)) performPageTurn(direction)
    }

    private fun performPageTurn(direction: Int) {
        val moved = if (direction > 0) {
            navigator?.goForward(animated = true) == true
        } else {
            navigator?.goBackward(animated = true) == true
        }
        if (!moved) toast(if (direction > 0) "已经到末尾" else "已经到开头")
        mainHandler.postDelayed({
            gestureGate.complete()?.let(::performPageTurn)
        }, PAGE_TURN_SETTLE_MS)
    }

    private fun applyReaderPreferences(updated: ReaderPreferences = readerPreferences) {
        val previous = readerPreferences
        readerPreferences = preferencesRepository.save(updated)
        navigator?.submitPreferences(
            preferencesRepository.toEpubPreferences(readerPreferences, readingProfile, isPortrait())
        )
        root.setBackgroundColor(readerBackgroundColor())
        publicationFrame.setBackgroundColor(readerBackgroundColor())
        ClickUi.applySystemBars(this, readerPreferences.theme == ReaderTheme.NIGHT)
        topChrome.setBackgroundColor(chromeBackgroundColor())
        bottomChrome.setBackgroundColor(chromeBackgroundColor())
        tintChromeText(topChrome, chromeTextColor())
        tintChromeText(bottomChrome, chromeTextColor())
        progressText.setTextColor(chromeMutedColor())
        if (::ttsMiniPlayer.isInitialized) {
            ttsMiniPlayer.background = rounded(
                ClickUi.elevated(this, readerPreferences.theme == ReaderTheme.NIGHT),
                dp(20),
                ClickUi.separator(this, readerPreferences.theme == ReaderTheme.NIGHT),
            )
            ttsMiniSentence.setTextColor(chromeTextColor())
            renderedTtsMiniSnapshot = null
            renderTtsMiniPlayer(ReaderTtsSession.current())
        }
        updatePublicationViewportPadding()
        if (previous.chineseScriptMode != readerPreferences.chineseScriptMode) {
            applyChineseScriptDisplay()
        }
        if (previous.theme != readerPreferences.theme) {
            scope.launch { refreshReadiumHighlights() }
        }
        setChromeVisible(chromeVisible)
    }

    private fun previewReaderPreferences(updated: ReaderPreferences, submitReadium: Boolean) {
        readerPreferences = updated
        if (submitReadium) {
            navigator?.submitPreferences(
                preferencesRepository.toEpubPreferences(readerPreferences, readingProfile, isPortrait()),
            )
        }
        updatePublicationViewportPadding()
    }

    private fun prepareViewportPreviewAnchor() {
        scope.launch {
            preparedViewportAnchor = captureViewportTextAnchor() ?: runCatching {
                Locator.fromJSON(JSONObject(lastLocatorJson.toString()))
            }.getOrNull()
        }
    }

    private fun beginViewportPreview() {
        pendingViewportAnchorRestore?.let(mainHandler::removeCallbacks)
        pendingViewportAnchorRestore = null
        viewportPreviewAnchor = preparedViewportAnchor ?: runCatching {
            Locator.fromJSON(JSONObject(lastLocatorJson.toString()))
        }.getOrNull()
    }

    private suspend fun captureViewportTextAnchor(): Locator? {
        val script = """
            (function(){
              var markerId='click-viewport-preview-anchor';
              function textAt(x,y){
                var range=null;
                if(document.caretRangeFromPoint){
                  range=document.caretRangeFromPoint(x,y);
                }else if(document.caretPositionFromPoint){
                  var position=document.caretPositionFromPoint(x,y);
                  if(position){
                    range=document.createRange();
                    range.setStart(position.offsetNode,position.offset);
                    range.collapse(true);
                  }
                }
                if(!range)return null;
                var node=range.startContainer,offset=range.startOffset;
                if(!node||node.nodeType!==Node.TEXT_NODE)return null;
                var value=node.nodeValue||'';
                while(offset<value.length&&/\s/.test(value.charAt(offset)))offset++;
                if(offset>=value.length)return null;
                var probe=document.createRange();
                probe.setStart(node,offset);
                probe.setEnd(node,Math.min(value.length,offset+1));
                var rect=probe.getBoundingClientRect();
                if(!(rect.width>0&&rect.height>0&&rect.bottom>0&&rect.top<window.innerHeight&&rect.right>0&&rect.left<window.innerWidth))return null;
                return {node:node,offset:offset,top:rect.top,left:rect.left};
              }
              var candidates=[];
              var ys=[2,8,16,28,42,58,76];
              for(var yi=0;yi<ys.length;yi++){
                for(var xi=8;xi<window.innerWidth;xi+=Math.max(24,Math.floor(window.innerWidth/10))){
                  var candidate=textAt(xi,ys[yi]);
                  if(candidate)candidates.push(candidate);
                }
                if(candidates.length)break;
              }
              if(!candidates.length)return '';
              candidates.sort(function(a,b){return a.top-b.top||a.left-b.left});
              var first=candidates[0],value=first.node.nodeValue||'',start=first.offset;
              var end=Math.min(value.length,start+48);
              var before=value.substring(Math.max(0,start-32),start);
              var highlight=value.substring(start,end);
              var after=value.substring(end,Math.min(value.length,end+32));
              var previous=document.getElementById(markerId);
              var alreadyAnchored=previous&&previous.nextSibling===first.node&&start===0;
              if(!alreadyAnchored){
                var tail=first.node.splitText(start);
                var marker=document.createElement('span');
                marker.id=markerId+'-next';
                marker.setAttribute('aria-hidden','true');
                marker.style.cssText='display:block!important;-webkit-column-break-before:always!important;break-before:column!important;width:0!important;height:0!important;margin:0!important;padding:0!important;border:0!important;';
                tail.parentNode.insertBefore(marker,tail);
                if(previous)previous.remove();
                marker.id=markerId;
              }
              return JSON.stringify({before:before,highlight:highlight,after:after});
            })();
        """.trimIndent()
        val raw = runCatching { navigator?.evaluateJavascript(script) }.getOrNull().orEmpty()
        val decoded = decodeJavascriptString(raw)
        val quote = runCatching { JSONObject(decoded) }.getOrNull() ?: return null
        if (quote.optString("highlight").isBlank()) return null
        val locatorJson = runCatching { JSONObject(lastLocatorJson.toString()) }.getOrNull() ?: return null
        locatorJson.put("locations", JSONObject())
        locatorJson.put("text", quote)
        return Locator.fromJSON(locatorJson)
    }

    private fun restoreViewportPreviewAnchor(commit: Boolean) {
        val anchor = viewportPreviewAnchor ?: return
        pendingViewportAnchorRestore?.let(mainHandler::removeCallbacks)
        val task = Runnable {
            pendingViewportAnchorRestore = null
            navigator?.go(anchor, animated = false)
            if (commit) {
                mainHandler.postDelayed({
                    if (viewportPreviewAnchor == anchor) {
                        viewportPreviewAnchor = null
                        lastLocatorJson = anchor.toJSON()
                        enqueueReadiumPosition(anchor)
                    }
                }, VIEWPORT_ANCHOR_SETTLE_MS)
            }
        }
        pendingViewportAnchorRestore = task
        mainHandler.postDelayed(
            task,
            if (commit) VIEWPORT_ANCHOR_COMMIT_DELAY_MS else VIEWPORT_ANCHOR_PREVIEW_DELAY_MS,
        )
    }

    private fun tintChromeText(view: View, color: Int) {
        if (view is TextView) view.setTextColor(color)
        if (view is ViewGroup) {
            for (index in 0 until view.childCount) tintChromeText(view.getChildAt(index), color)
        }
    }

    private fun readerBackgroundColor(): Int =
        if (readerPreferences.theme == ReaderTheme.NIGHT) Color.rgb(16, 17, 15) else Color.rgb(244, 241, 232)

    private fun chromeBackgroundColor(): Int =
        ClickUi.surface(this, readerPreferences.theme == ReaderTheme.NIGHT)

    private fun chromeTextColor(): Int =
        ClickUi.primaryLabel(this, readerPreferences.theme == ReaderTheme.NIGHT)

    private fun chromeMutedColor(): Int =
        ClickUi.secondaryLabel(this, readerPreferences.theme == ReaderTheme.NIGHT)

    private fun readerDialogBuilder(): AlertDialog.Builder =
        AlertDialog.Builder(
            this,
            if (readerPreferences.theme == ReaderTheme.NIGHT) {
                R.style.ClickDialogTheme
            } else {
                R.style.ClickDialogLightTheme
            },
        )

    private fun openReadiumPublication() {
        val file = File(epubPath)
        if (!file.exists()) {
            toast("本地 EPUB 缓存不存在")
            finish()
            return
        }
        scope.launch {
            val result = withContext(Dispatchers.IO) {
                runCatching {
                    val httpClient = DefaultHttpClient()
                    val assetRetriever = AssetRetriever(contentResolver, httpClient)
                    val parser = DefaultPublicationParser(this@ClickReadiumNavigatorActivity, httpClient, assetRetriever, null)
                    val opener = PublicationOpener(parser)
                    val openedAsset = assetRetriever.retrieve(file).getOrNull()
                        ?: error("Readium cannot retrieve EPUB asset")
                    val openedPublication = opener.open(openedAsset, allowUserInteraction = false).getOrNull()
                        ?: error("Readium cannot open EPUB publication")
                    openedAsset to openedPublication
                }
            }
            result.onSuccess { (openedAsset, openedPublication) ->
                asset = openedAsset
                publication = openedPublication
                installNavigator(openedPublication)
            }.onFailure {
                toast("Readium 打开失败，返回 HTML 离线阅读：${it.compact()}")
                finish()
            }
        }
    }

    private fun installNavigator(openedPublication: org.readium.r2.shared.publication.Publication) {
        val restoredLocator = restoredReadiumLocator(openedPublication)
        val initialPreferences = preferencesRepository.toEpubPreferences(readerPreferences, readingProfile, isPortrait())
        val factory = EpubNavigatorFactory(
            openedPublication,
            EpubNavigatorFactory.Configuration()
        ).createFragmentFactory(
            restoredLocator,
            openedPublication.readingOrder,
            initialPreferences,
            this,
            this,
            EpubNavigatorFragment.Configuration(
                selectionActionModeCallback = selectionActionModeCallback,
                // Click owns the fullscreen viewport. Readium's automatic padding duplicated
                // the 128 px display-cutout inset and pushed visible text down to ~233 px.
                shouldApplyInsetsPadding = false,
            )
        )
        supportFragmentManager.fragmentFactory = factory
        supportFragmentManager.commit {
            replace(containerId, EpubNavigatorFragment::class.java, Bundle(), READIUM_FRAGMENT_TAG)
        }
        scope.launch {
            val fragment = waitForNavigator()
            navigator = fragment
            readerTitle.text = openedPublication.metadata.title.orEmpty().trim().ifBlank { "阅读" }
            fragment.addInputListener(readerInputListener)
            fragment.submitPreferences(initialPreferences)
            applyImportedFontToCurrentPage()
            applyChineseScriptDisplay()
            refreshReadiumHighlights()
            setChromeVisible(false)
            if (showTocOnLaunch) {
                showTocOnLaunch = false
                showReadiumTocDialog()
            }
            scope.launch {
                fragment.currentLocator.collectLatest { locator ->
                    if (viewportPreviewAnchor == null) {
                        lastLocatorJson = locator.toJSON()
                        enqueueReadiumPosition(locator)
                    }
                }
            }
        }
        toast("Readium 已打开")
    }

    private fun isPortrait(): Boolean = resources.configuration.orientation != Configuration.ORIENTATION_LANDSCAPE

    private fun showReadiumTocDialog(initialMode: Int = 0) {
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(8), dp(4), dp(8), dp(4))
        }
        val tabs = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        val search = EditText(this).apply {
            hint = "搜索目录"
            isSingleLine = true
            textSize = 15f
            setPadding(dp(12), 0, dp(12), 0)
            val dark = readerPreferences.theme == ReaderTheme.NIGHT
            setTextColor(ClickUi.primaryLabel(this@ClickReadiumNavigatorActivity, dark))
            setHintTextColor(ClickUi.secondaryLabel(this@ClickReadiumNavigatorActivity, dark))
            background = ClickUi.rounded(
                ClickUi.elevated(this@ClickReadiumNavigatorActivity, dark),
                dp(14).toFloat(),
                ClickUi.separator(this@ClickReadiumNavigatorActivity, dark),
                dp(1),
            )
        }
        val scroll = ScrollView(this)
        val content = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        scroll.addView(content)
        panel.addView(tabs, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48)))
        panel.addView(search, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(44)))
        panel.addView(scroll, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(430)))

        lateinit var dialog: AlertDialog
        var currentMode = 0
        val tabButtons = mutableListOf<Button>()
        fun emptyRow(message: String) {
            content.addView(
                TextView(this).apply {
                    text = message
                    textSize = 15f
                    setTextColor(chromeMutedColor())
                    gravity = Gravity.CENTER
                    setPadding(dp(10), dp(24), dp(10), dp(24))
                },
                LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT),
            )
        }
        fun render(mode: Int) {
            currentMode = mode
            tabButtons.forEachIndexed { index, button ->
                ClickUi.stylePill(
                    button,
                    index == mode,
                    readerPreferences.theme == ReaderTheme.NIGHT,
                )
            }
            search.visibility = if (mode == 0) View.VISIBLE else View.GONE
            content.removeAllViews()
            when (mode) {
                0 -> {
                    val rawLinks = publication?.tableOfContents?.takeIf { it.isNotEmpty() }
                        ?: publication?.readingOrder
                        ?: emptyList()
                    val links = compactTocRoots(rawLinks)
                    if (links.isEmpty()) emptyRow("这本书没有可用目录")
                    val currentHref = normalizedTocHref(lastLocatorJson.optString("href"))
                    expandCurrentTocBranch(links, currentHref)
                    val rows = mutableListOf<TocEntry>()
                    collectTocEntries(links, 0, search.text?.toString().orEmpty(), rows)
                    if (links.isNotEmpty() && rows.isEmpty()) emptyRow("没有匹配的目录")
                    rows.forEach { entry ->
                        val link = entry.link
                        val key = normalizedTocHref(link.href.toString())
                        val current = currentHref == key
                        val row = LinearLayout(this).apply {
                            orientation = LinearLayout.HORIZONTAL
                            gravity = Gravity.TOP
                            setPadding(dp(entry.depth * 16), 0, 0, 0)
                            minimumHeight = dp(50)
                        }
                        if (link.children.isNotEmpty()) {
                            row.addView(
                                tocExpandButton(if (expandedTocHrefs.contains(key)) "⌄" else "›") {
                                    if (!expandedTocHrefs.add(key)) expandedTocHrefs.remove(key)
                                    render(0)
                                    true
                                },
                                LinearLayout.LayoutParams(dp(42), dp(50)),
                            )
                        } else {
                            row.addView(View(this), LinearLayout.LayoutParams(dp(42), dp(50)))
                        }
                        val title = buildString {
                            if (current) append("●  ")
                            append(link.title?.trim().orEmpty().ifBlank { link.href.toString() })
                        }
                        row.addView(
                            tocRow(title) {
                                val before = lastLocatorJson.toString()
                                if (navigator?.go(link, animated = true) == true) dialog.dismiss()
                                else if (link.children.isNotEmpty()) {
                                    if (!expandedTocHrefs.add(key)) expandedTocHrefs.remove(key)
                                    render(0)
                                } else {
                                    lastLocatorJson = JSONObject(before)
                                    toast("章节跳转失败，阅读位置未改变")
                                }
                                true
                            }.apply {
                                if (current) {
                                    setTextColor(
                                        ClickUi.accent(
                                            this@ClickReadiumNavigatorActivity,
                                            readerPreferences.theme == ReaderTheme.NIGHT,
                                        ),
                                    )
                                }
                            },
                            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f),
                        )
                        content.addView(
                            row,
                            LinearLayout.LayoutParams(
                                ViewGroup.LayoutParams.MATCH_PARENT,
                                ViewGroup.LayoutParams.WRAP_CONTENT,
                            ),
                        )
                    }
                }
                1 -> {
                    content.addView(primaryButton("添加当前位置书签") {
                        enqueueBookmark()
                        render(1)
                        true
                    })
                    val rows = store.annotations(bookId, "bookmark")
                    if (rows.isEmpty()) emptyRow("暂无书签")
                    rows.forEach { row ->
                        content.addView(annotationListRow(row, dialog) { render(1) })
                    }
                }
                else -> {
                    val activeSyncIssues = store.syncIssueCount(bookId)
                    if (activeSyncIssues > 0) {
                        content.addView(primaryButton("⚠ 本书有 $activeSyncIssues 项同步问题 · 去处理") {
                            dialog.dismiss()
                            openSyncIssues()
                            true
                        })
                    }
                    val rows = store.annotations(bookId, "").filter { it.kind != "bookmark" }
                    if (rows.isEmpty()) emptyRow("暂无标红或备注")
                    rows.forEach { row ->
                        val prefix = when (row.kind) {
                            "note" -> if (isVoiceNote(row)) "语音" else "备注"
                            "audio_note" -> "语音"
                            else -> "标红"
                        }
                        val value = row.noteText.ifBlank { row.sourceText }.ifBlank { row.chapterLocator }
                        content.addView(annotationListRow(row, dialog, "$prefix · ${value.take(72)}") { render(2) })
                    }
                }
            }
        }
        listOf("目录", "书签", "批注").forEachIndexed { index, label ->
            val button = pillButton(label) { render(index); true }
            tabButtons += button
            tabs.addView(button, LinearLayout.LayoutParams(0, dp(44), 1f))
        }
        search.addTextChangedListener(object : TextWatcher {
            override fun beforeTextChanged(value: CharSequence?, start: Int, count: Int, after: Int) = Unit
            override fun onTextChanged(value: CharSequence?, start: Int, before: Int, count: Int) {
                if (currentMode == 0) render(0)
            }
            override fun afterTextChanged(value: Editable?) = Unit
        })
        dialog = readerDialogBuilder()
            .setView(panel)
            .setNegativeButton("关闭", null)
            .create()
        render(initialMode.coerceIn(0, 2))
        dialog.show()
    }

    private fun compactTocRoots(links: List<Link>): List<Link> {
        if (links.size != 1) return links
        val root = links.first()
        if (root.children.isEmpty()) return links
        val rootTitle = root.title.orEmpty().trim().lowercase(java.util.Locale.ROOT)
        val bookTitle = publication?.metadata?.title.orEmpty().trim().lowercase(java.util.Locale.ROOT)
        val redundantWrapper = rootTitle.isNotEmpty() && (
            rootTitle == bookTitle ||
                rootTitle == "目录" ||
                rootTitle == "contents" ||
                rootTitle == "table of contents"
            )
        return if (redundantWrapper) root.children else links
    }

    private fun openSyncIssues() {
        startActivity(Intent(this, ClickNativeReaderActivity::class.java).apply {
            putExtra(ClickNativeReaderActivity.EXTRA_BASE_URL, baseUrl)
            putExtra(ClickNativeReaderActivity.EXTRA_DEVICE_ID, deviceId)
            putExtra(ClickNativeReaderActivity.EXTRA_ACCESS_TOKEN, accessToken)
            putExtra(ClickNativeReaderActivity.EXTRA_OPEN_SYNC_ISSUES, true)
        })
    }

    private fun tocRow(label: String, action: () -> Boolean): Button =
        button(label.trim(), action).apply {
            textSize = 15f
            gravity = Gravity.START or Gravity.CENTER_VERTICAL
            textAlignment = View.TEXT_ALIGNMENT_VIEW_START
            setSingleLine(false)
            maxLines = Int.MAX_VALUE
            ellipsize = null
            setTextColor(chromeTextColor())
            setBackgroundColor(Color.TRANSPARENT)
            setPadding(dp(12), dp(8), dp(8), dp(8))
            minHeight = dp(50)
            minimumHeight = dp(50)
        }

    private fun annotationListRow(
        row: ClickNativeStore.AnnotationRow,
        parentDialog: AlertDialog,
        label: String = row.sourceText.ifBlank { row.chapterLocator },
        afterChanged: () -> Unit,
    ): LinearLayout = LinearLayout(this).apply {
        orientation = LinearLayout.HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        addView(
            tocRow(label) {
                goToAnnotation(row)
                parentDialog.dismiss()
                true
            },
            LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f),
        )
        addView(
            button("⋮") {
                showAnnotationActions(row, parentDialog, afterChanged)
                true
            }.apply {
                textSize = 24f
                contentDescription = "管理${annotationKindLabel(row)}"
                setTextColor(chromeMutedColor())
                setBackgroundColor(Color.TRANSPARENT)
                minWidth = 0
                minimumWidth = 0
                setPadding(0, 0, 0, 0)
            },
            LinearLayout.LayoutParams(dp(50), dp(50)),
        )
    }

    private fun annotationKindLabel(row: ClickNativeStore.AnnotationRow): String = when {
        row.kind == "bookmark" -> "书签"
        isVoiceNote(row) -> "语音备注"
        row.kind == "note" -> "备注"
        else -> "标红"
    }

    private fun showAnnotationActions(
        row: ClickNativeStore.AnnotationRow,
        parentDialog: AlertDialog,
        afterChanged: () -> Unit,
    ) {
        val actions = mutableListOf("定位")
        if (row.kind == "note" || isVoiceNote(row)) actions.add("编辑备注")
        if (isVoiceNote(row)) actions.add(if (activeVoiceNoteId == voicePlaybackKey(row)) "停止语音" else "播放语音")
        actions.add("删除")
        val summary = row.noteText.ifBlank { row.sourceText }.take(80)
        readerDialogBuilder()
            // AlertDialog does not reliably render both a message and a choice list.
            .setTitle(listOf(annotationKindLabel(row), summary).filter { it.isNotBlank() }.joinToString(" · "))
            .setItems(actions.toTypedArray()) { _, index ->
                when (actions[index]) {
                    "定位" -> {
                        goToAnnotation(row)
                        parentDialog.dismiss()
                    }
                    "编辑备注" -> showEditAnnotationDialog(row, afterChanged)
                    "播放语音" -> playVoiceNote(row)
                    "停止语音" -> {
                        releaseVoiceNotePlayer()
                        toast("语音已停止")
                    }
                    "删除" -> confirmDeleteAnnotation(row, afterChanged)
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun showEditAnnotationDialog(row: ClickNativeStore.AnnotationRow, afterChanged: () -> Unit) {
        val input = EditText(this).apply {
            minLines = 3
            setText(row.noteText)
            setSelection(text.length)
            setTextColor(Color.BLACK)
        }
        readerDialogBuilder()
            .setTitle("编辑备注")
            .setView(input)
            .setPositiveButton("保存") { _, _ -> updateAnnotationNote(row, input.text.toString(), afterChanged) }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun updateAnnotationNote(
        row: ClickNativeStore.AnnotationRow,
        noteText: String,
        afterChanged: () -> Unit,
    ) {
        scope.launch {
            val result = runCatching {
                withContext(Dispatchers.IO) {
                    store.updateAnnotationNoteAndEnqueue(
                        operationId(),
                        row.id,
                        bookId,
                        noteText,
                        row.serverVersion,
                    )
                    ClickSyncScheduler.enqueueMetadata(this@ClickReadiumNavigatorActivity)
                }
            }
            result.onSuccess {
                afterChanged()
                toast("备注已保存")
            }.onFailure { toast("备注保存失败：${it.compact()}") }
        }
    }

    private fun confirmDeleteAnnotation(row: ClickNativeStore.AnnotationRow, afterChanged: () -> Unit) {
        readerDialogBuilder()
            .setTitle("删除${annotationKindLabel(row)}？")
            .setMessage("只删除这条记录；关联的原始录音文件不会被静默删除。")
            .setPositiveButton("删除") { _, _ -> deleteAnnotation(row, afterChanged) }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun deleteAnnotation(row: ClickNativeStore.AnnotationRow, afterChanged: () -> Unit) {
        scope.launch {
            val result = runCatching {
                withContext(Dispatchers.IO) {
                    store.deleteAnnotationAndEnqueue(
                        operationId(),
                        row.id,
                        bookId,
                        row.serverVersion,
                    )
                    ClickSyncScheduler.enqueueMetadata(this@ClickReadiumNavigatorActivity)
                }
            }
            result.onSuccess {
                if (activeVoiceNoteId == voicePlaybackKey(row)) releaseVoiceNotePlayer()
                refreshReadiumHighlights()
                afterChanged()
                toast("${annotationKindLabel(row)}已删除")
            }.onFailure { toast("删除失败：${it.compact()}") }
        }
    }

    private fun isVoiceNote(row: ClickNativeStore.AnnotationRow): Boolean {
        if (row.kind == "audio_note") return true
        return runCatching {
            val metadata = JSONObject(row.metadataJson)
            metadata.optString("action") == "readium_audio_note" || metadata.optJSONObject("voice_note") != null
        }.getOrDefault(false)
    }

    private fun voiceNoteId(row: ClickNativeStore.AnnotationRow): String = runCatching {
        JSONObject(row.metadataJson).optJSONObject("voice_note")?.optString("audio_note_id").orEmpty()
    }.getOrDefault("")

    private fun voicePlaybackKey(row: ClickNativeStore.AnnotationRow): String =
        voiceNoteId(row).ifBlank { row.id }

    private fun playVoiceNote(row: ClickNativeStore.AnnotationRow) {
        val audioNoteId = voiceNoteId(row)
        val playbackKey = voicePlaybackKey(row)
        if (activeVoiceNoteId == playbackKey) {
            releaseVoiceNotePlayer()
            toast("语音已停止")
            return
        }
        releaseVoiceNotePlayer()
        activeVoiceNoteId = playbackKey
        scope.launch {
            val result = runCatching {
                withContext(Dispatchers.IO) {
                    val local = audioNoteRepository.resolve(JSONObject(row.metadataJson))
                    when {
                        local != null -> VoicePlaybackSource(local, false)
                        audioNoteId.isBlank() -> error("语音仍在上传或处理中")
                        !hasRemoteEndpoint() -> error("离线状态下没有可用的本地语音")
                        else -> VoicePlaybackSource(downloadVoiceNoteAudio(audioNoteId), true)
                    }
                }
            }
            result.onSuccess { source ->
                if (activeVoiceNoteId != playbackKey) {
                    if (source.temporary) source.file.delete()
                    return@onSuccess
                }
                activeVoicePlaybackFile = source.file
                activeVoicePlaybackTemporary = source.temporary
                val player = MediaPlayer()
                activeVoicePlayer = player
                runCatching {
                    player.setDataSource(source.file.absolutePath)
                    player.setOnPreparedListener {
                        it.start()
                        toast("正在播放语音备注")
                    }
                    player.setOnCompletionListener {
                        releaseVoiceNotePlayer()
                        toast("语音播放完成")
                    }
                    player.setOnErrorListener { _, _, _ ->
                        releaseVoiceNotePlayer()
                        toast("语音播放失败")
                        true
                    }
                    player.prepareAsync()
                }.onFailure {
                    releaseVoiceNotePlayer()
                    toast("语音播放失败：${it.compact()}")
                }
            }
            result.onFailure {
                releaseVoiceNotePlayer()
                toast("语音播放失败：${it.compact()}")
            }
        }
    }

    private fun downloadVoiceNoteAudio(audioNoteId: String): File {
        val audioUrl = ReaderApiUrlPolicy.resolve(
            baseUrl,
            "/v1/android/audio-notes/${encode(audioNoteId)}/audio",
        )
        val target = File.createTempFile("click-voice-note-", ".audio", cacheDir)
        val connection = URL(audioUrl).openConnection() as HttpURLConnection
        connection.instanceFollowRedirects = false
        connection.connectTimeout = 8_000
        connection.readTimeout = 90_000
        connection.requestMethod = "GET"
        connection.setRequestProperty("X-Click-Device-Id", deviceId)
        if (accessToken.isNotBlank()) {
            connection.setRequestProperty("X-Click-Access-Token", accessToken)
            connection.setRequestProperty("Authorization", "Bearer $accessToken")
        }
        try {
            val code = connection.responseCode
            check(code in 200..299) { "HTTP $code" }
            connection.inputStream.use { input ->
                FileOutputStream(target).use { output ->
                    input.copyTo(output)
                    output.fd.sync()
                }
            }
            check(target.length() > 0L) { "服务返回了空音频" }
            return target
        } catch (error: Throwable) {
            target.delete()
            throw error
        } finally {
            connection.disconnect()
        }
    }

    private fun releaseVoiceNotePlayer() {
        activeVoicePlayer?.runCatching { stop() }
        activeVoicePlayer?.runCatching { release() }
        activeVoicePlayer = null
        if (activeVoicePlaybackTemporary) activeVoicePlaybackFile?.delete()
        activeVoicePlaybackFile = null
        activeVoicePlaybackTemporary = false
        activeVoiceNoteId = ""
    }

    private data class VoicePlaybackSource(val file: File, val temporary: Boolean)

    private fun tocExpandButton(label: String, action: () -> Boolean): Button =
        button(label, action).apply {
            textSize = 24f
            setTextColor(chromeMutedColor())
            setBackgroundColor(Color.TRANSPARENT)
            minWidth = 0
            minimumWidth = 0
            minHeight = 0
            minimumHeight = 0
            setPadding(0, 0, 0, 0)
        }

    private data class TocEntry(val link: Link, val depth: Int)

    private fun normalizedTocHref(value: String): String =
        value.substringBefore('#').removePrefix("./").trimStart('/')

    private fun expandCurrentTocBranch(links: List<Link>, currentHref: String): Boolean {
        var found = false
        links.forEach { link ->
            val key = normalizedTocHref(link.href.toString())
            val inChild = expandCurrentTocBranch(link.children, currentHref)
            val matches = key.isNotBlank() && key == currentHref
            if (inChild) expandedTocHrefs.add(key)
            if (matches || inChild) found = true
        }
        return found
    }

    private fun collectTocEntries(links: List<Link>, depth: Int, rawQuery: String, output: MutableList<TocEntry>) {
        val query = rawQuery.trim().lowercase()
        links.forEach { link ->
            val title = link.title?.trim().orEmpty().ifBlank { link.href.toString() }
            val key = normalizedTocHref(link.href.toString())
            if (query.isNotBlank()) {
                if (title.lowercase().contains(query)) output.add(TocEntry(link, depth))
                collectTocEntries(link.children, depth + 1, query, output)
            } else {
                output.add(TocEntry(link, depth))
                if (expandedTocHrefs.contains(key)) collectTocEntries(link.children, depth + 1, query, output)
            }
        }
    }

    private fun enqueueBookmark() {
        val opId = operationId()
        val href = lastLocatorJson.optString("href")
        val payload = JSONObject()
            .put("book_id", bookId)
            .put("kind", "bookmark")
            .put("source_text", lastLocatorJson.optString("title").ifBlank { href })
            .put("note_text", "")
            .put("color", "")
            .put("chapter_locator", href)
            .put("range_locator", lastLocatorJson)
            .put("metadata", androidMetadata("bookmark"))
        store.saveLocalAnnotationAndEnqueue(opId, "annotation_created", bookId, payload)
        ClickSyncScheduler.enqueueMetadata(this)
        toast("书签已保存 · ${store.pendingCount()} 条待同步")
    }

    private fun goToAnnotation(row: ClickNativeStore.AnnotationRow) {
        val locator = runCatching { Locator.fromJSON(JSONObject(row.rangeLocatorJson)) }.getOrNull()
        if (locator != null && navigator?.go(locator, animated = true) == true) return
        val link = (publication?.tableOfContents.orEmpty() + publication?.readingOrder.orEmpty())
            .firstOrNull { it.href.toString() == row.chapterLocator }
        if (link == null || navigator?.go(link, animated = true) != true) toast("这条批注暂时无法定位")
    }

    private fun showReadiumVisualDialog() {
        lateinit var dialog: AlertDialog
        prepareViewportPreviewAnchor()
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(10), dp(18), dp(8))
            setBackgroundColor(chromeBackgroundColor())
        }
        fun addVisualSeek(
            label: String,
            minimum: Int,
            maximum: Int,
            current: Int,
            submitReadium: Boolean = true,
            updated: (ReaderPreferences, Int) -> ReaderPreferences,
        ) {
            panel.addView(
                preferenceSeekRow(
                    label = label,
                    minimum = minimum,
                    maximum = maximum,
                    current = current,
                    onStartTracking = if (submitReadium) null else ::beginViewportPreview,
                    onPreview = { value ->
                        previewReaderPreferences(updated(readerPreferences, value), submitReadium)
                        if (!submitReadium) restoreViewportPreviewAnchor(commit = false)
                    },
                    onChanged = { value ->
                        applyReaderPreferences(updated(readerPreferences, value))
                        if (!submitReadium) restoreViewportPreviewAnchor(commit = true)
                    },
                ),
            )
        }

        fun addSection(label: String) {
            panel.addView(
                TextView(this).apply {
                    text = label
                    textSize = 13f
                    setTypeface(android.graphics.Typeface.DEFAULT_BOLD)
                    setTextColor(chromeMutedColor())
                    gravity = Gravity.BOTTOM
                    setPadding(0, dp(8), 0, dp(2))
                },
                LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(34)),
            )
        }

        addSection("文字")
        panel.addView(pillButton("字体与简繁") {
            showReadiumFontDialog {}
            true
        })
        addVisualSeek("字号", 16, 36, readerPreferences.fontSizeSp) { preferences, value ->
            preferences.copy(fontSizeSp = value)
        }
        addVisualSeek("字重", 80, 160, readerPreferences.fontWeightPercent) { preferences, value ->
            preferences.copy(fontWeightPercent = value)
        }
        addVisualSeek("字距", 0, 30, readerPreferences.letterSpacingPercent) { preferences, value ->
            preferences.copy(letterSpacingPercent = value)
        }

        addSection("段落")
        addVisualSeek("行距", 100, 200, readerPreferences.lineHeightPercent) { preferences, value ->
            preferences.copy(lineHeightPercent = value)
        }
        addVisualSeek("段距", 0, 100, readerPreferences.paragraphGapPercent) { preferences, value ->
            preferences.copy(paragraphGapPercent = value)
        }

        addSection("页面")
        addVisualSeek(
            "上边距",
            0,
            48,
            readerPreferences.viewportTopDp,
            submitReadium = false,
        ) { preferences, value ->
            preferences.copy(viewportTopDp = value)
        }
        addVisualSeek(
            "下边距",
            0,
            48,
            readerPreferences.viewportBottomDp,
            submitReadium = false,
        ) { preferences, value ->
            preferences.copy(viewportBottomDp = value)
        }
        addVisualSeek(
            "左边距",
            0,
            48,
            readerPreferences.viewportLeftDp,
            submitReadium = false,
        ) { preferences, value ->
            preferences.copy(viewportLeftDp = value)
        }
        addVisualSeek(
            "右边距",
            0,
            48,
            readerPreferences.viewportRightDp,
            submitReadium = false,
        ) { preferences, value ->
            preferences.copy(viewportRightDp = value)
        }

        if (readingProfile == "IMAGE_COMIC" || readingProfile == "FIXED_LAYOUT") {
            val pageModeRow = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER
            }
            listOf(
                "自动分页" to ComicPageMode.AUTO,
                "单页" to ComicPageMode.SINGLE,
                "双页" to ComicPageMode.DOUBLE,
            ).forEach { (label, mode) ->
                pageModeRow.addView(pillButton(label) {
                    applyReaderPreferences(readerPreferences.copy(comicPageMode = mode))
                    toast(
                        when (mode) {
                            ComicPageMode.AUTO -> "竖屏单页，横屏和平板双页"
                            ComicPageMode.SINGLE -> "漫画固定单页"
                            ComicPageMode.DOUBLE -> "漫画固定双页"
                        },
                    )
                    true
                }, LinearLayout.LayoutParams(0, dp(42), 1f))
            }
            panel.addView(pageModeRow, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(46)))
        }

        lateinit var progressButton: Button
        progressButton = pillButton(if (readerPreferences.showProgress) "隐藏页码" else "显示页码") {
            applyReaderPreferences(readerPreferences.copy(showProgress = !readerPreferences.showProgress))
            progressText.visibility = if (readerPreferences.showProgress) View.VISIBLE else View.GONE
            progressButton.text = if (readerPreferences.showProgress) "隐藏页码" else "显示页码"
            true
        }
        panel.addView(progressButton)
        panel.addView(pillButton("恢复默认排版") {
            readerPreferences = preferencesRepository.resetLayout()
            applyReaderPreferences(readerPreferences)
            progressText.visibility = if (readerPreferences.showProgress) View.VISIBLE else View.GONE
            dialog.dismiss()
            mainHandler.post(::showReadiumVisualDialog)
            true
        })

        val scroll = ScrollView(this).apply { addView(panel) }
        dialog = readerDialogBuilder()
            .setTitle("排版")
            .setView(scroll)
            .setNegativeButton("关闭", null)
            .create()
        dialog.show()
    }

    private fun showReadiumFontDialog(onChanged: () -> Unit) {
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(10), dp(18), dp(8))
            setBackgroundColor(chromeBackgroundColor())
        }
        fun currentFontLabel(): String {
            val status = fontManager.resolve()
            return when {
                status.resolvedFamily == ReaderFontManager.IMPORTED_FONT_FAMILY ->
                    status.resolvedLabel.removePrefix("用户导入 · ").ifBlank { "导入字体" }
                status.importedFile != null -> "导入字体未启用"
                else -> "系统中文字体"
            }
        }
        val currentFont = TextView(this).apply {
            text = "当前 · ${currentFontLabel()}"
            textSize = 14f
            setTextColor(chromeMutedColor())
            setPadding(0, 0, 0, dp(8))
        }
        panel.addView(currentFont)

        val scriptRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        listOf(
            "原文" to ChineseScriptMode.ORIGINAL,
            "简体" to ChineseScriptMode.SIMPLIFIED,
            "繁体" to ChineseScriptMode.TRADITIONAL,
        ).forEach { (label, mode) ->
            scriptRow.addView(pillButton(label) {
                if (mode != ChineseScriptMode.ORIGINAL && displayVariantPayload.optJSONObject("resources") == null) {
                    toast("这本书尚未缓存简繁映射，请连接 Mac 后同步全部")
                } else {
                    applyReaderPreferences(readerPreferences.copy(chineseScriptMode = mode))
                    onChanged()
                }
                true
            }, LinearLayout.LayoutParams(0, dp(42), 1f))
        }
        panel.addView(scriptRow, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(46)))
        panel.addView(pillButton("导入字体") {
            launchReaderFontImport()
            true
        })
        if (fontManager.resolve().importedFile != null) {
            panel.addView(pillButton("移除导入字体") {
                if (fontManager.removeImportedFont()) {
                    applyReaderPreferences(readerPreferences)
                    currentFont.text = "当前 · ${currentFontLabel()}"
                    onChanged()
                    toast("已恢复系统中文字体")
                }
                true
            })
        }

        readerDialogBuilder()
            .setTitle("字体")
            .setView(panel)
            .setNegativeButton("关闭", null)
            .show()
    }

    private fun showReadiumMoreDialog() {
        val endpointConfigured = hasRemoteEndpoint()
        val credentialsConfigured = accessToken.isNotBlank()
        val networkAvailable = hasInternetCapability()
        val askAiLabel = when {
            !endpointConfigured || !credentialsConfigured -> "问 AI · 需配对 Mac"
            !networkAvailable -> "问 AI · 当前离线"
            else -> "问 AI · 基于本书和笔记"
        }
        val items = arrayOf("书签", "批注", askAiLabel, "同步状态", "书籍信息", "返回书架")
        readerDialogBuilder()
            .setTitle("更多")
            .setItems(items) { _, which ->
                when (which) {
                    0 -> showReadiumTocDialog(1)
                    1 -> showReadiumTocDialog(2)
                    2 -> when {
                        !endpointConfigured || !credentialsConfigured ->
                            toast("问 AI 需要已配对的 Mac")
                        !networkAvailable ->
                            toast("当前离线，问 AI 暂不可用")
                        else -> showHermesBookQuestionDialog()
                    }
                    3 -> {
                        val pending = store.pendingCount() + store.pendingBookImportCount()
                        val issues = store.syncIssueCount(bookId)
                        toast(if (issues > 0) "本书有 $issues 项同步问题" else "待同步 $pending 项")
                    }
                    4 -> readerDialogBuilder()
                        .setTitle("书籍信息")
                        .setMessage("本地 EPUB\n${File(epubPath).name}\n\n书籍 ID\n$bookId")
                        .setPositiveButton("关闭", null)
                        .show()
                    5 -> finish()
                }
            }
            .show()
    }

    private fun performSelectionAction(index: Int, onSelectionCaptured: () -> Unit) {
        val fragment = navigator ?: run {
            toast("阅读器尚未准备好")
            onSelectionCaptured()
            return
        }
        scope.launch {
            val locator = fragment.currentSelection()?.locator
            if (locator == null || selectionText(locator).isBlank()) {
                onSelectionCaptured()
                return@launch
            }
            onSelectionCaptured()
            when (index) {
                0 -> {
                    copyText(selectionText(locator))
                    fragment.clearSelection()
                }
                1 -> enqueueSelectionAnnotation("red_highlight_created", "red_highlight", locator, "", "red")
                2 -> {
                    fragment.clearSelection()
                    showNoteDialog(locator)
                }
                3 -> {
                    fragment.clearSelection()
                    speakText(selectionText(locator))
                }
                4 -> {
                    fragment.clearSelection()
                    lookup(selectionWord(locator))
                }
                5 -> {
                    fragment.clearSelection()
                    chooseAudioNote(locator)
                }
                6 -> {
                    fragment.clearSelection()
                    askHermes(selectionText(locator))
                }
            }
        }
    }

    private fun showHermesBookQuestionDialog() {
        val question = EditText(this).apply {
            hint = "你想问这本书什么？"
            minLines = 2
            maxLines = 6
        }
        readerDialogBuilder()
            .setTitle("问 AI")
            .setMessage("基于本书已索引正文和笔记回答；需要已配对且可达的 Mac。证据不足会明确说明。")
            .setView(question)
            .setNegativeButton("取消", null)
            .setNeutralButton("问当前句") { _, _ -> askHermesCurrentSentence() }
            .setPositiveButton("发送") { _, _ ->
                askHermesBookQuestion(question.text.toString())
            }
            .show()
    }

    private fun askHermesCurrentSentence() {
        val currentSentence = ReaderTtsSession.currentTextForBook(bookId)
        if (currentSentence.isBlank()) {
            toast("请先开始朗读；当前句入口只使用正在播放的这一句")
            return
        }
        askHermesContext("当前朗读句", currentSentence)
    }

    private fun askHermesBookQuestion(rawQuestion: String) {
        val question = HermesBookQuestionContract.cleanQuestion(rawQuestion)
        if (question.isBlank()) {
            toast("请先输入关于这本书的问题")
            return
        }
        val endpointConfigured = hasRemoteEndpoint()
        val credentialsConfigured = accessToken.isNotBlank()
        if (!endpointConfigured || !credentialsConfigured) {
            toast("Hermes 需要已配对的 Mac 连接")
            return
        }
        if (
            HermesEvidenceStore.isPairedOffline(
                endpointConfigured,
                credentialsConfigured,
                hasInternetCapability(),
            )
        ) {
            HermesEvidenceStore.recordPairedOfflineBlocked(this, bookId)
            toast("当前离线；问题没有发送，也不会启动本地大模型")
            return
        }
        val currentBook = readerTitle.text.toString().trim().ifBlank { "当前书籍" }
        val currentChapterHref = lastLocatorJson.optString("href").trim()
        val currentChapterTitle = lastLocatorJson.optString("title").trim().ifBlank {
            currentChapterHref.ifBlank { "当前章节" }
        }
        val message = HermesBookQuestionContract.buildMessage(
            HermesBookQuestionContext(
                bookId = bookId,
                bookTitle = currentBook,
                chapterTitle = currentChapterTitle,
                locatorHref = currentChapterHref,
            ),
            question,
        )
        toast("Hermes 正在回答关于《$currentBook》的问题")
        scope.launch {
            val result = runCatching {
                withContext(Dispatchers.IO) {
                    JSONObject(
                        request(
                            "POST",
                            "$baseUrl/v1/runtime/chat",
                            JSONObject()
                                .put("message", message)
                                .put("context_scope", "current_book")
                                .put("book_id", bookId)
                                .put("chapter_locator", currentChapterHref)
                                .put("question", question)
                                .put("locator", lastLocatorJson),
                        ),
                    )
                }
            }
            result.onSuccess { payload ->
                if (payload.optBoolean("ok", false)) {
                    HermesEvidenceStore.recordSuccessfulReceipt(
                        this@ClickReadiumNavigatorActivity,
                        payload.optJSONObject("receipt"),
                    )
                }
                val reply = payload.optString("reply").ifBlank {
                    payload.optString("error", "Hermes 暂时没有返回内容")
                }.replace("**", "").replace(Regex("(?m)^>\\s?"), "")
                readerDialogBuilder()
                    .setTitle("Hermes · 当前书")
                    .setMessage(reply)
                    .setPositiveButton("关闭", null)
                    .show()
            }.onFailure { toast("Hermes 暂不可用：${it.compact()}") }
        }
    }

    private fun askHermes(selectedText: String) {
        askHermesContext("所选原文", selectedText)
    }

    private fun askHermesContext(contextLabel: String, contextText: String) {
        val cleaned = contextText.trim()
        if (cleaned.isBlank()) {
            toast("请先选择要交给 Hermes 的文字")
            return
        }
        if (!hasRemoteEndpoint() || accessToken.isBlank()) {
            toast("Hermes 需要连接你的 Mac")
            return
        }
        toast("Hermes 正在理解${contextLabel}")
        scope.launch {
            val currentBook = readerTitle.text.toString().trim().ifBlank { "当前书籍" }
            val currentChapter = lastLocatorJson.optString("title").trim().ifBlank {
                lastLocatorJson.optString("href").trim().ifBlank { "当前章节" }
            }
            val result = runCatching {
                withContext(Dispatchers.IO) {
                    val body = JSONObject().put(
                        "message",
                        "你正在处理 Click 阅读器当前页面。请只依据下面明确给出的书籍、章节和原文回答，不要沿用会话中其他书籍的上下文。\n" +
                            "书籍：$currentBook\n章节：$currentChapter\n$contextLabel：$cleaned\n\n" +
                            "请用简洁中文解释这段原文；证据不足时直接说明，不要猜测它与其他书的关系。",
                    )
                    JSONObject(request("POST", "$baseUrl/v1/runtime/chat", body))
                }
            }
            result.onSuccess { payload ->
                val reply = payload.optString("reply").ifBlank {
                    payload.optString("error", "Hermes 暂时没有返回内容")
                }.replace("**", "").replace(Regex("(?m)^>\\s?"), "")
                readerDialogBuilder()
                    .setTitle("Hermes")
                    .setMessage(reply)
                    .setPositiveButton("关闭", null)
                    .show()
            }.onFailure { toast("Hermes 暂不可用：${it.compact()}") }
        }
    }

    private fun restoredReadiumLocator(
        openedPublication: org.readium.r2.shared.publication.Publication,
    ): Locator? = runCatching {
        val raw = pendingTtsResumeLocatorJson.takeIf { it.isNotBlank() }
            ?: store.positionLocatorJson(bookId).takeIf { it.isNotBlank() }
            ?: return@runCatching null
        val json = locatorJson(raw)
        Locator.fromJSON(json)?.takeIf { it.href.toString().isNotBlank() }?.let {
            return@runCatching it
        }

        // Older Click/Lite positions predate Readium and store the chapter href separately.
        val legacyHref = json.optString("chapterLocator")
            .ifBlank { json.optString("chapter_locator") }
            .trim()
        if (legacyHref.isBlank()) return@runCatching null

        fun normalizedHref(value: String): String = value.substringBefore('#').removePrefix("./").trimStart('/')
        val normalizedLegacyHref = normalizedHref(legacyHref)
        val matchingLink = openedPublication.readingOrder.firstOrNull { link ->
            val candidate = normalizedHref(link.href.toString())
            candidate == normalizedLegacyHref ||
                candidate.endsWith("/$normalizedLegacyHref") ||
                normalizedLegacyHref.endsWith("/$candidate")
        }
        val pageIndex = json.optInt("pageIndex", 0).coerceAtLeast(0)
        val totalPages = json.optInt("totalPages", 1).coerceAtLeast(1)
        val progression = (pageIndex.toDouble() / totalPages.toDouble()).coerceIn(0.0, 0.999999)
        val restoredUrl = Url(matchingLink?.href?.toString() ?: legacyHref) ?: return@runCatching null
        Locator(
            restoredUrl,
            matchingLink?.mediaType ?: MediaType.XHTML,
            matchingLink?.title ?: json.optString("chapterTitle"),
            Locator.Locations(progression = progression),
        )
    }.getOrNull()

    private fun locatorJson(raw: String): JSONObject {
        val cleaned = raw.trim()
        if (cleaned.startsWith("{")) {
            runCatching { JSONObject(cleaned) }.getOrNull()?.let { return it }
        }
        return JSONObject().put("chapterLocator", cleaned)
    }

    private fun showReadiumTtsDialog() {
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(10), dp(18), dp(8))
            setBackgroundColor(chromeBackgroundColor())
        }
        val status = TextView(this).apply {
            textSize = 15f
            setTextColor(chromeTextColor())
            setPadding(0, 0, 0, dp(8))
            maxLines = 3
            ellipsize = android.text.TextUtils.TruncateAt.END
        }
        panel.addView(status)

        val startButton = primaryButton("开始朗读") {
            startTtsFromSelectionOrCurrentPage()
            true
        }.apply {
            contentDescription = "朗读选中或当前页"
        }
        panel.addView(startButton)

        lateinit var playPause: Button
        val controls = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        controls.addView(
            miniTtsIconButton(R.drawable.ic_click_previous, "上一句") { ReaderTtsSession.previous() },
            LinearLayout.LayoutParams(0, dp(42), 1f),
        )
        playPause = miniTtsIconButton(
            R.drawable.ic_click_pause,
            "暂停/继续",
        ) { ReaderTtsSession.pauseOrResume() }
        controls.addView(playPause, LinearLayout.LayoutParams(0, dp(42), 1f))
        controls.addView(
            miniTtsIconButton(R.drawable.ic_click_next, "下一句") { ReaderTtsSession.next() },
            LinearLayout.LayoutParams(0, dp(42), 1f),
        )
        panel.addView(
            controls,
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(46)),
        )
        val optionsRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        val timerButtons = linkedMapOf<Int, Button>()
        fun renderTimerButtons() {
            timerButtons.forEach { (minutes, button) ->
                val selected = readerPreferences.ttsTimerMinutes == minutes
                button.text = if (selected) "⏱$minutes" else "$minutes"
                button.contentDescription = buildString {
                    append("$minutes 分钟倒计时")
                    if (selected) append("，当前选择")
                }
            }
        }
        intArrayOf(30, 60, 90).forEach { minutes ->
            val button = pillButton("$minutes") {
                applyTtsTimer(minutes)
                renderTimerButtons()
                true
            }
            timerButtons[minutes] = button
            optionsRow.addView(button, LinearLayout.LayoutParams(0, dp(42), 1f))
        }
        optionsRow.addView(pillButton("设置") {
            showReadiumTtsSettingsDialog()
            true
        }, LinearLayout.LayoutParams(0, dp(42), 1f))
        renderTimerButtons()
        panel.addView(optionsRow, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(46)))

        fun isActive(state: ReaderTtsState): Boolean =
            state == ReaderTtsState.PREPARING ||
                state == ReaderTtsState.PLAYING ||
                state == ReaderTtsState.PAUSED

        fun renderState(state: ReaderTtsSnapshot = ReaderTtsSession.current()) {
            val active = isActive(state.state)
            val primary = when {
                state.currentText.isNotBlank() -> state.currentText
                state.state == ReaderTtsState.ERROR -> state.message.ifBlank { "朗读没有启动，请检查设置" }
                state.state == ReaderTtsState.PREPARING -> "正在准备朗读…"
                readerPreferences.ttsMode == ReaderTtsMode.MICROSOFT_ONLINE ->
                    "微软在线 · 需要 Mac 和网络\n从选中文字或当前页开始"
                else -> "手机离线 · 不需要 Mac 或网络\n从选中文字或当前页开始"
            }
            val bufferLine = if (
                active &&
                readerPreferences.ttsMode == ReaderTtsMode.MICROSOFT_ONLINE
            ) {
                when {
                    state.bufferedToBookEnd -> "微软缓存：已到全书结尾 · ${state.bufferedMinutes} 分钟"
                    state.bufferFilling -> "微软缓存：${state.bufferedMinutes} 分钟，正在补到 90"
                    else -> "微软缓存：还可听 ${state.bufferedMinutes} 分钟"
                }
            } else {
                ""
            }
            status.text = listOf(primary, bufferLine).filter(String::isNotBlank).joinToString("\n")
            controls.visibility = if (active) View.VISIBLE else View.GONE
            startButton.visibility = if (active) View.GONE else View.VISIBLE
            val paused = state.state == ReaderTtsState.PAUSED
            ClickUi.styleIconButton(
                playPause,
                if (paused) R.drawable.ic_click_play else R.drawable.ic_click_pause,
                if (paused) "继续播放" else "暂停",
                readerPreferences.theme == ReaderTheme.NIGHT,
            )
        }

        val listener = ReaderTtsSession.Listener { state -> runOnUiThread { renderState(state) } }
        val dialog = readerDialogBuilder()
            .setTitle("朗读")
            .setView(panel)
            .setNegativeButton("关闭", null)
            .create()
        dialog.setOnShowListener { ReaderTtsSession.addListener(listener) }
        dialog.setOnDismissListener { ReaderTtsSession.removeListener(listener) }
        renderState()
        dialog.show()
    }

    private fun showReadiumTtsSettingsDialog() {
        lateinit var dialog: AlertDialog
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(10), dp(18), dp(8))
            setBackgroundColor(chromeBackgroundColor())
        }
        val localVoices = localTtsModelManager.installedVoices()
        val selectedLocalVoice = localTtsModelManager.selectedVoice(
            readerPreferences.ttsLocalModelId,
            readerPreferences.ttsLocalVoiceId,
        )

        val modeDescription = TextView(this).apply {
            textSize = 13f
            setTextColor(chromeMutedColor())
            setPadding(0, dp(4), 0, dp(8))
        }
        val modeRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        lateinit var offlineButton: Button
        lateinit var onlineButton: Button
        fun renderMode() {
            val online = readerPreferences.ttsMode == ReaderTtsMode.MICROSOFT_ONLINE
            offlineButton.text = if (online) "手机离线" else "✓ 手机离线"
            onlineButton.text = if (online) "✓ 微软在线" else "微软在线"
            modeDescription.text = if (online) {
                "需要 Mac 和网络，朗读文字会发送给微软"
            } else {
                "全部在手机处理，不需要 Mac 或网络"
            }
        }
        offlineButton = pillButton("手机离线") {
            readerPreferences = preferencesRepository.save(
                readerPreferences.copy(ttsMode = ReaderTtsMode.LOCAL_PRIVATE),
            )
            renderMode()
            true
        }
        onlineButton = pillButton("微软在线") {
            readerPreferences = preferencesRepository.save(
                readerPreferences.copy(ttsMode = ReaderTtsMode.MICROSOFT_ONLINE),
            )
            renderMode()
            true
        }
        modeRow.addView(offlineButton, LinearLayout.LayoutParams(0, dp(42), 1f))
        modeRow.addView(onlineButton, LinearLayout.LayoutParams(0, dp(42), 1f))
        panel.addView(modeRow, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(46)))
        panel.addView(modeDescription)

        val voiceRow = LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER
        }
        val currentVoice = selectedLocalVoice ?: localVoices.firstOrNull()
        lateinit var voiceButton: Button
        voiceButton = pillButton(
            currentVoice?.let { "声音 · ${it.displayName}" } ?: "声音 · Android 系统离线",
        ) {
            if (localVoices.isEmpty()) {
                openSystemTtsSettings()
            } else {
                val labels = localVoices.map { it.displayName }.toTypedArray()
                val checked = localVoices.indexOfFirst { it.voiceId == readerPreferences.ttsLocalVoiceId }
                    .coerceAtLeast(0)
                readerDialogBuilder()
                    .setTitle("离线声音")
                    .setSingleChoiceItems(labels, checked) { choice, which ->
                        val voice = localVoices[which]
                        readerPreferences = preferencesRepository.save(
                            readerPreferences.copy(
                                ttsLocalModelId = voice.modelId,
                                ttsLocalVoiceId = voice.voiceId,
                                ttsMode = ReaderTtsMode.LOCAL_PRIVATE,
                            ),
                        )
                        voiceButton.text = "声音 · ${voice.displayName}"
                        renderMode()
                        choice.dismiss()
                        previewOfflineTts()
                    }
                    .show()
            }
            true
        }
        voiceRow.addView(voiceButton, LinearLayout.LayoutParams(0, dp(42), 3f))
        voiceRow.addView(
            pillButton("试听") {
                previewOfflineTts()
                true
            },
            LinearLayout.LayoutParams(0, dp(42), 1f),
        )
        panel.addView(voiceRow, LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(46)))

        panel.addView(pillButton("语音管理") {
            showOfflineVoiceManagementDialog(currentVoice)
            true
        })
        renderMode()

        panel.addView(preferenceSeekRow("语速", 80, 150, readerPreferences.ttsRatePercent) { value ->
            readerPreferences = preferencesRepository.save(readerPreferences.copy(ttsRatePercent = value))
            ReaderTtsSession.setRate(value)
        })

        val currentState = ReaderTtsSession.current().state
        if (
            currentState == ReaderTtsState.PREPARING ||
            currentState == ReaderTtsState.PLAYING ||
            currentState == ReaderTtsState.PAUSED
        ) {
            panel.addView(pillButton("停止朗读") {
                ReaderTtsSession.stop()
                dialog.dismiss()
                true
            })
        }

        val scroll = ScrollView(this).apply { addView(panel) }
        dialog = readerDialogBuilder()
            .setTitle("朗读设置")
            .setView(scroll)
            .setNegativeButton("关闭", null)
            .create()
        activeTtsSettingsDialog?.takeIf { it !== dialog }?.dismiss()
        activeTtsSettingsDialog = dialog
        dialog.setOnDismissListener {
            if (activeTtsSettingsDialog === dialog) activeTtsSettingsDialog = null
        }
        dialog.show()
    }

    private fun previewOfflineTts() {
        ensureTtsNotificationPermission()
        ReaderTtsSession.start(
            this,
            listOf("这是 Click 手机离线朗读。"),
            readerPreferences.copy(ttsMode = ReaderTtsMode.LOCAL_PRIVATE),
            ttsNetworkConfig(),
        )
    }

    private fun openSystemTtsSettings() {
        runCatching {
            startActivity(Intent(TextToSpeech.Engine.ACTION_INSTALL_TTS_DATA))
        }.onFailure {
            toast("系统没有提供离线语音入口，请到系统的文字转语音设置中检查")
        }
    }

    private fun showOfflineVoiceManagementDialog(currentVoice: LocalTtsVoice?) {
        val actions = mutableListOf("系统离线语音设置", "导入高质量语音包")
        if (currentVoice != null) actions += "删除高质量语音包"
        readerDialogBuilder()
            .setTitle("语音管理")
            .setItems(actions.toTypedArray()) { _, which ->
                when (actions[which]) {
                    "系统离线语音设置" -> openSystemTtsSettings()
                    "导入高质量语音包" -> launchLocalTtsModelImport()
                    "删除高质量语音包" -> {
                        readerDialogBuilder()
                            .setTitle("删除高质量语音包？")
                            .setMessage("只删除手机里的语音模型，不会删除书籍或批注。")
                            .setPositiveButton("删除") { _, _ ->
                                ReaderTtsSession.unloadLocalModel()
                                if (localTtsModelManager.removeModel(checkNotNull(currentVoice).modelId)) {
                                    toast("高质量语音包已删除")
                                } else {
                                    toast("语音包删除失败")
                                }
                            }
                            .setNegativeButton("取消", null)
                            .show()
                    }
                }
            }
            .setNegativeButton("关闭", null)
            .show()
    }

    private suspend fun waitForNavigator(): EpubNavigatorFragment {
        while (true) {
            val fragment = supportFragmentManager.findFragmentByTag(READIUM_FRAGMENT_TAG)
            if (fragment is EpubNavigatorFragment) return fragment
            kotlinx.coroutines.delay(50)
        }
    }

    private fun withSelection(label: String, action: (Locator) -> Unit): Boolean {
        val fragment = navigator
        if (fragment == null) {
            toast("Readium 尚未准备好")
            return false
        }
        scope.launch {
            val selection = fragment.currentSelection()
            val locator = selection?.locator
            if (locator == null || selectionText(locator).isBlank()) {
                toast("请先选中文字再使用$label")
            } else {
                action(locator)
            }
        }
        return true
    }

    private fun enqueueSelectionAnnotation(operationType: String, kind: String, locator: Locator, noteText: String, color: String) {
        scope.launch {
            val saved = runCatching {
                withContext(Dispatchers.IO) {
                    val displayText = selectionText(locator)
                    val sourceText = sourceTextForDisplaySnapshot(displayText, locator.href.toString())
                    val payload = JSONObject()
                        .put("book_id", bookId)
                        .put("kind", kind)
                        .put("source_text", sourceText)
                        .put("note_text", noteText)
                        .put("color", color)
                        .put("chapter_locator", locator.href.toString())
                        .put("range_locator", locator.toJSON().put("mode", "readium_selection"))
                        .put("metadata", androidMetadata(kind, displayText))
                    val opId = operationId()
                    store.saveLocalAnnotationAndEnqueue(opId, operationType, bookId, payload)
                    ClickSyncScheduler.enqueueMetadata(this@ClickReadiumNavigatorActivity)
                    store.pendingCount()
                }
            }.getOrElse {
                toast("保存失败：${it.compact()}")
                return@launch
            }
            navigator?.clearSelection()
            if (kind == "red_highlight") refreshReadiumHighlights()
            toast("已保存 · $saved 条待同步")
        }
    }

    private suspend fun refreshReadiumHighlights() {
        val fragment = navigator ?: return
        val rows = withContext(Dispatchers.IO) { store.annotations(bookId, "red_highlight") }
        val decorations = rows.mapNotNull { row ->
            runCatching { Locator.fromJSON(JSONObject(row.rangeLocatorJson)) }.getOrNull()?.let { locator ->
                Decoration(
                    row.id,
                    locator,
                    Decoration.Style.Highlight(highlightTint(), false),
                )
            }
        }
        fragment.applyDecorations(decorations, RED_HIGHLIGHT_DECORATION_GROUP)
    }

    private fun highlightTint(): Int =
        if (readerPreferences.theme == ReaderTheme.NIGHT) Color.argb(150, 126, 49, 49)
        else Color.argb(145, 239, 94, 94)

    private fun showNoteDialog(locator: Locator) {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            pendingVoiceNoteLocatorJson = locator.toJSON().toString()
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQUEST_RECORD_AUDIO_PERMISSION)
            return
        }
        if (Build.VERSION.SDK_INT < 31 || !SpeechRecognizer.isOnDeviceRecognitionAvailable(this)) {
            toast("本机没有可用的离线识别，已改为录音保存")
            showAudioRecorder(locator)
            return
        }

        releaseNoteRecognizer()
        val input = EditText(this).apply {
            minLines = 3
            setTextColor(Color.BLACK)
            hint = "直接说话；识别结果会显示在这里"
        }
        val status = TextView(this).apply {
            text = "正在启动手机离线语音识别…"
            textSize = 13f
            setTextColor(chromeTextColor())
            setPadding(0, dp(8), 0, dp(8))
        }
        val stopButton = primaryButton("结束并保存") { true }
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(8), dp(18), dp(6))
            addView(input)
            addView(status)
            addView(stopButton)
        }
        val dialog = readerDialogBuilder()
            .setTitle("备注 · 离线语音")
            .setMessage(selectionText(locator))
            .setView(panel)
            .setNegativeButton("取消", null)
            .create()
        val recognizer = SpeechRecognizer.createOnDeviceSpeechRecognizer(this)
        activeNoteRecognizer = recognizer
        activeNoteDialog = dialog
        recognizer.setRecognitionListener(object : RecognitionListener {
            override fun onReadyForSpeech(params: Bundle?) {
                if (activeNoteDialog !== dialog) return
                activeNoteListening = true
                status.text = "正在听。说完停顿后会自动保存"
                stopButton.isEnabled = true
            }

            override fun onBeginningOfSpeech() {
                if (activeNoteDialog === dialog) status.text = "正在识别…"
            }

            override fun onRmsChanged(rmsdB: Float) = Unit
            override fun onBufferReceived(buffer: ByteArray?) = Unit

            override fun onEndOfSpeech() {
                if (activeNoteDialog !== dialog) return
                activeNoteListening = false
                status.text = "正在整理文字…"
                stopButton.isEnabled = false
            }

            override fun onError(error: Int) {
                if (activeNoteDialog !== dialog) return
                activeNoteListening = false
                status.text = voiceRecognitionError(error)
                stopButton.text = if (input.text.toString().isBlank()) "重新说" else "保存文字"
                stopButton.isEnabled = true
            }

            override fun onResults(results: Bundle?) {
                if (activeNoteDialog !== dialog) return
                activeNoteListening = false
                val text = speechResult(results).ifBlank { input.text.toString().trim() }
                if (text.isBlank()) {
                    status.text = "没有识别出文字，可以重新说或使用键盘"
                    stopButton.text = "重新说"
                    stopButton.isEnabled = true
                    return
                }
                input.setText(text)
                enqueueSelectionAnnotation("note_created", "note", locator, text, "")
                dialog.dismiss()
            }

            override fun onPartialResults(partialResults: Bundle?) {
                if (activeNoteDialog !== dialog) return
                speechResult(partialResults).takeIf(String::isNotBlank)?.let(input::setText)
            }

            override fun onEvent(eventType: Int, params: Bundle?) = Unit
        })
        stopButton.setOnClickListener {
            when {
                activeNoteListening -> {
                    stopButton.isEnabled = false
                    status.text = "正在整理文字…"
                    recognizer.stopListening()
                }
                input.text.toString().isNotBlank() -> {
                    enqueueSelectionAnnotation(
                        "note_created",
                        "note",
                        locator,
                        input.text.toString().trim(),
                        "",
                    )
                    dialog.dismiss()
                }
                else -> startOfflineNoteRecognition(recognizer, status, stopButton)
            }
        }
        dialog.setOnShowListener {
            startOfflineNoteRecognition(recognizer, status, stopButton)
        }
        dialog.setOnDismissListener {
            if (activeNoteDialog === dialog) releaseNoteRecognizer()
        }
        dialog.show()
    }

    private fun showKeyboardNoteDialog(locator: Locator) {
        val input = EditText(this).apply {
            minLines = 3
            setTextColor(Color.BLACK)
        }
        readerDialogBuilder()
            .setTitle("备注")
            .setMessage(selectionText(locator))
            .setView(input)
            .setPositiveButton("保存") { _, _ ->
                input.text.toString().trim().takeIf(String::isNotBlank)?.let { note ->
                    enqueueSelectionAnnotation("note_created", "note", locator, note, "")
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun startOfflineNoteRecognition(
        recognizer: SpeechRecognizer,
        status: TextView,
        button: Button,
    ) {
        val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
            putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)
            putExtra(RecognizerIntent.EXTRA_LANGUAGE, "zh-CN")
            putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
            putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 3)
            putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, true)
        }
        activeNoteListening = false
        status.text = "正在启动手机离线语音识别…"
        button.text = "结束并保存"
        button.isEnabled = false
        runCatching { recognizer.startListening(intent) }
            .onFailure {
                status.text = "离线识别启动失败，可以使用键盘"
                button.text = "保存文字"
                button.isEnabled = true
            }
    }

    private fun speechResult(results: Bundle?): String =
        results
            ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
            ?.firstOrNull()
            .orEmpty()
            .trim()

    private fun voiceRecognitionError(error: Int): String = when (error) {
        SpeechRecognizer.ERROR_NO_MATCH -> "没有识别出文字，可以重新说"
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "没有听到声音，可以重新说"
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "麦克风权限不可用，可以使用键盘"
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "识别器正忙，请稍后重试"
        else -> "离线识别没有完成，可以重新说或使用键盘"
    }

    private fun releaseNoteRecognizer() {
        activeNoteListening = false
        activeNoteRecognizer?.runCatching { cancel() }
        activeNoteRecognizer?.runCatching { destroy() }
        activeNoteRecognizer = null
        activeNoteDialog = null
    }

    private fun chooseAudioNote(locator: Locator) {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            pendingAudioLocatorJson = locator.toJSON().toString()
            requestPermissions(arrayOf(Manifest.permission.RECORD_AUDIO), REQUEST_RECORD_AUDIO_PERMISSION)
            return
        }
        showAudioRecorder(locator)
    }

    private fun showAudioRecorder(locator: Locator) {
        releaseRecorder(deleteFile = true)
        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(18), dp(10), dp(18), dp(8))
        }
        val status = TextView(this).apply {
            text = "准备录音\n${selectionText(locator).take(72)}"
            textSize = 15f
            setTextColor(chromeTextColor())
            setPadding(0, 0, 0, dp(10))
        }
        val recordButton = primaryButton("开始录音") { true }
        panel.addView(status)
        panel.addView(recordButton)
        val dialog = readerDialogBuilder()
            .setTitle("语音备注")
            .setView(panel)
            .setNegativeButton("取消", null)
            .create()
        recordButton.setOnClickListener {
            if (activeRecorder == null) {
                runCatching { startRecorder() }
                    .onSuccess {
                        status.text = "正在录音，点下方按钮结束并立即保存"
                        recordButton.text = "结束并保存"
                    }
                    .onFailure {
                        releaseRecorder(deleteFile = true)
                        toast("录音启动失败：${it.compact()}")
                    }
            } else {
                val file = runCatching { stopRecorder() }
                    .onFailure { toast("录音保存失败：${it.compact()}") }
                    .getOrNull()
                if (file != null) enqueueRecordedAudioNote(file, locator)
                dialog.dismiss()
            }
        }
        dialog.setOnDismissListener {
            if (activeRecorder != null) releaseRecorder(deleteFile = true)
        }
        dialog.show()
    }

    @Suppress("DEPRECATION")
    private fun startRecorder() {
        val file = audioNoteRepository.createRecordingTarget()
        val recorder = if (Build.VERSION.SDK_INT >= 31) MediaRecorder(this) else MediaRecorder()
        recorder.setAudioSource(MediaRecorder.AudioSource.MIC)
        recorder.setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
        recorder.setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
        recorder.setAudioEncodingBitRate(96_000)
        recorder.setAudioSamplingRate(44_100)
        recorder.setOutputFile(file.absolutePath)
        recorder.prepare()
        recorder.start()
        activeAudioFile = file
        activeRecorder = recorder
    }

    private fun stopRecorder(): File {
        val recorder = requireNotNull(activeRecorder)
        val file = requireNotNull(activeAudioFile)
        recorder.stop()
        recorder.release()
        activeRecorder = null
        activeAudioFile = null
        require(file.isFile && file.length() > 256) { "录音文件无效" }
        return file
    }

    private fun releaseRecorder(deleteFile: Boolean) {
        activeRecorder?.runCatching { stop() }
        activeRecorder?.runCatching { release() }
        activeRecorder = null
        if (deleteFile) activeAudioFile?.delete()
        activeAudioFile = null
    }

    private fun enqueueRecordedAudioNote(file: File, locator: Locator) {
        scope.launch {
            val pending = runCatching {
                withContext(Dispatchers.IO) {
                    val opId = operationId()
                    val localAudio = audioNoteRepository.commit(file, "audio/mp4")
                    val displayText = selectionText(locator)
                    val source = sourceTextForDisplaySnapshot(displayText, locator.href.toString())
                    val uploadMetadata = androidMetadata("readium_audio_note", displayText)
                    val localMetadata = audioNoteRepository.metadata(
                        uploadMetadata,
                        localAudio,
                    )
                    val payload = JSONObject()
                        .put("book_id", bookId)
                        .put("audio_base64", android.util.Base64.encodeToString(file.readBytes(), android.util.Base64.NO_WRAP))
                        .put("mime_type", "audio/mp4")
                        .put("source_text", source)
                        .put("chapter_locator", locator.href.toString())
                        .put("range_locator", locator.toJSON().put("mode", "readium_audio_note"))
                        .put("metadata", uploadMetadata)
                    val localAnnotation = JSONObject(payload.toString())
                        .put("kind", "audio_note")
                        .put("note_text", "语音备注待转写")
                        .put("color", "")
                        .put("metadata", localMetadata)
                    store.saveLocalAnnotationAndEnqueue(
                        opId,
                        "audio_note_created",
                        bookId,
                        localAnnotation,
                        payload,
                    )
                    ClickSyncScheduler.enqueueMetadata(this@ClickReadiumNavigatorActivity)
                    store.pendingCount()
                }
            }.getOrElse {
                audioNoteRepository.delete(file)
                toast("语音备注保存失败：${it.compact()}")
                return@launch
            }
            navigator?.clearSelection()
            toast("语音备注已保存 · $pending 条待同步")
        }
    }

    private fun lookup(word: String) {
        scope.launch {
            val cached = withContext(Dispatchers.IO) { store.lookupCache(bookId, word) }
                .takeIf(String::isNotBlank)
                ?.let { runCatching { JSONObject(it) }.getOrNull() }
            if (cached != null) {
                showLookupResult(word, cached, "本地缓存")
                if (hasRemoteEndpoint()) scope.launch(Dispatchers.IO) {
                    runCatching {
                        val url = "$baseUrl/v1/android/books/${encode(bookId)}/lookup?word=${encode(word)}"
                        JSONObject(request("GET", url, null)).also { store.saveLookupCache(bookId, word, it) }
                    }
                }
                return@launch
            }
            if (!hasRemoteEndpoint()) {
                toast("离线状态下没有这个词的本地缓存")
                return@launch
            }

            val remote = runCatching {
                withContext(Dispatchers.IO) {
                    val url = "$baseUrl/v1/android/books/${encode(bookId)}/lookup?word=${encode(word)}"
                    JSONObject(request("GET", url, null)).also { store.saveLookupCache(bookId, word, it) }
                }
            }
            remote.onSuccess { showLookupResult(word, it, "Click 词库") }
                .onFailure { toast("查词失败：${it.compact()}") }
        }
    }

    private fun showLookupResult(word: String, payload: JSONObject, sourceLabel: String) {
        val meaning = payload.optJSONObject("meaning")
        val definition = meaning?.optString("zh").orEmpty()
            .ifBlank { payload.optString("context_meaning_zh") }
        val partOfSpeech = payload.optString("part_of_speech_zh")
        val message = listOf(partOfSpeech, definition, sourceLabel)
            .filter(String::isNotBlank)
            .joinToString("\n")
            .ifBlank { "未查到释义" }
        readerDialogBuilder()
            .setTitle(word)
            .setMessage(message)
            .setNeutralButton("读词") { _, _ -> speakText(word) }
            .setPositiveButton("关闭", null)
            .show()
    }

    private fun speakText(text: String) {
        if (text.isBlank()) {
            toast("没有可朗读内容")
            return
        }
        ensureTtsNotificationPermission()
        registerTtsResumeTarget()
        ReaderTtsSession.start(
            this,
            ReaderTtsTextNormalizer.segment(text),
            readerPreferences,
            ttsNetworkConfig(),
        )
    }

    private fun startTtsFromSelectionOrCurrentPage() {
        scope.launch {
            val selected = navigator?.currentSelection()?.locator?.let(::selectionText).orEmpty()
            val segments = if (selected.isNotBlank()) {
                ReaderTtsTextNormalizer.segment(selected)
            } else {
                currentPageSegments()
            }
            if (segments.isEmpty()) {
                toast("当前页没有取得可朗读正文")
                return@launch
            }
            ensureTtsNotificationPermission()
            registerTtsResumeTarget()
            if (selected.isBlank()) {
                ReaderTtsSession.start(
                    this@ClickReadiumNavigatorActivity,
                    segments,
                    readerPreferences,
                    ttsNetworkConfig(),
                    continueAcrossPages = true,
                )
            } else {
                ReaderTtsSession.start(this@ClickReadiumNavigatorActivity, segments, readerPreferences, ttsNetworkConfig())
            }
            navigator?.clearSelection()
        }
    }

    private fun requestNextTtsPage(onReady: (List<String>) -> Unit) {
        requestAdjacentTtsPage(1, onReady)
    }

    private fun requestPreviousTtsPage(onReady: (List<String>) -> Unit) {
        requestAdjacentTtsPage(-1, onReady)
    }

    private fun requestAdjacentTtsPage(direction: Int, onReady: (List<String>) -> Unit) {
        scope.launch {
            val before = lastLocatorJson.toString()
            val moved = if (direction > 0) {
                navigator?.goForward(animated = false) == true
            } else {
                navigator?.goBackward(animated = false) == true
            }
            if (!moved) {
                onReady(emptyList())
                return@launch
            }
            repeat(50) {
                kotlinx.coroutines.delay(50)
                if (lastLocatorJson.toString() != before) {
                    kotlinx.coroutines.delay(100)
                    onReady(currentPageSegments())
                    return@launch
                }
            }
            onReady(emptyList())
        }
    }

    private suspend fun currentPageSegments(): List<String> {
        val script = """
            (function(){
              var blocks=document.querySelectorAll('h1,h2,h3,p,li,blockquote');
              var text=[],seen={},total=0;
              function visible(range){
                var rects=range.getClientRects();
                for(var r=0;r<rects.length;r++){
                  var rect=rects[r];
                  if(rect.width>0&&rect.height>0&&rect.bottom>0&&rect.top<window.innerHeight&&rect.right>0&&rect.left<window.innerWidth)return true;
                }
                return false;
              }
              for(var bi=0;bi<blocks.length&&total<5000;bi++){
                var walker=document.createTreeWalker(blocks[bi],NodeFilter.SHOW_TEXT,null,false);
                var chars=[],refs=[],node;
                while((node=walker.nextNode())){
                  var value=node.nodeValue||'';
                  for(var i=0;i<value.length;i++){
                    var ch=value.charAt(i);
                    if(ch==='\u00a0')ch=' ';
                    if(ch==='—'||ch==='–')ch='，';
                    if(ch==='\n'||ch==='\r'){
                      if(chars.length&&chars[chars.length-1]!=='\n'){chars.push('\n');refs.push({node:node,offset:i});}
                    }else if(/\s/.test(ch)){
                      if(chars.length&&chars[chars.length-1]!==' '){chars.push(' ');refs.push({node:node,offset:i});}
                    }else{chars.push(ch);refs.push({node:node,offset:i});}
                  }
                }
                while(chars.length&&chars[chars.length-1]===' '){chars.pop();refs.pop();}
                var start=0;
                for(var ci=0;ci<=chars.length;ci++){
                  var boundary=ci===chars.length||chars[ci]==='\n'||/[。！？；.!?;]/.test(chars[ci]);
                  if(!boundary)continue;
                  var end=ci===chars.length?ci:ci+1;
                  while(start<end&&chars[start]===' ')start++;
                  while(end>start&&chars[end-1]===' ')end--;
                  if(end>start){
                    var range=document.createRange();
                    range.setStart(refs[start].node,refs[start].offset);
                    range.setEnd(refs[end-1].node,refs[end-1].offset+1);
                    var sentence=chars.slice(start,end).join('');
                    if(visible(range)&&!seen[sentence]){text.push(sentence);seen[sentence]=true;total+=sentence.length;}
                  }
                  start=ci+1;
                }
              }
              return JSON.stringify(text);
            })();
        """.trimIndent()
        val raw = navigator?.evaluateJavascript(script).orEmpty()
        val decoded = decodeJavascriptString(raw)
        return runCatching {
            val array = JSONArray(decoded)
            (0 until array.length())
                .map { array.optString(it) }
                .flatMap(ReaderTtsTextNormalizer::segment)
                .filter(String::isNotBlank)
        }.getOrDefault(emptyList())
    }

    private fun decodeJavascriptString(value: String): String {
        val trimmed = value.trim()
        if (!trimmed.startsWith('"')) return trimmed
        return runCatching { JSONObject("{\"value\":$trimmed}").getString("value") }.getOrDefault(trimmed)
    }

    private fun updateTtsTextHighlight(state: ReaderTtsSnapshot) {
        if (navigator == null) return
        val text = when (state.state) {
            ReaderTtsState.PREPARING, ReaderTtsState.PLAYING, ReaderTtsState.PAUSED -> state.currentText
            else -> ""
        }
        if (text == lastTtsHighlightText) return
        lastTtsHighlightText = text
        val target = JSONObject.quote(text)
        val color = if (readerPreferences.theme == ReaderTheme.NIGHT) {
            "rgba(174,89,67,0.38)"
        } else {
            "rgba(246,195,70,0.34)"
        }
        val script = """
            (function(target,color){
              var layerId='click-tts-current-sentence';
              var previous=document.getElementById(layerId);
              if(previous)previous.remove();
              if(!target)return false;
              function normalize(value){
                return (value||'').replace(/\u00a0/g,' ').replace(/[—–]/g,'，').replace(/\s+/g,' ').trim();
              }
              var needle=normalize(target);
              if(!needle)return false;
              var blocks=Array.prototype.slice.call(document.querySelectorAll('h1,h2,h3,p,li,blockquote'));
              blocks.sort(function(a,b){
                var ar=a.getBoundingClientRect(),br=b.getBoundingClientRect();
                var av=ar.bottom>0&&ar.top<window.innerHeight&&ar.right>0&&ar.left<window.innerWidth?0:1;
                var bv=br.bottom>0&&br.top<window.innerHeight&&br.right>0&&br.left<window.innerWidth?0:1;
                return av-bv;
              });
              for(var bi=0;bi<blocks.length;bi++){
                var walker=document.createTreeWalker(blocks[bi],NodeFilter.SHOW_TEXT,null,false);
                var chars=[],refs=[],node;
                while((node=walker.nextNode())){
                  var value=node.nodeValue||'';
                  for(var i=0;i<value.length;i++){
                    var ch=value.charAt(i);
                    if(ch==='\u00a0')ch=' ';
                    if(ch==='—'||ch==='–')ch='，';
                    if(/\s/.test(ch)){
                      if(chars.length&&chars[chars.length-1]!==' '){chars.push(' ');refs.push({node:node,offset:i});}
                    }else{chars.push(ch);refs.push({node:node,offset:i});}
                  }
                }
                while(chars.length&&chars[chars.length-1]===' '){chars.pop();refs.pop();}
                var index=chars.join('').indexOf(needle);
                if(index<0)continue;
                var first=refs[index],last=refs[index+needle.length-1];
                if(!first||!last)continue;
                var range=document.createRange();
                range.setStart(first.node,first.offset);
                range.setEnd(last.node,last.offset+1);
                var rects=Array.prototype.slice.call(range.getClientRects()).filter(function(rect){
                  return rect.width>0&&rect.height>0&&rect.bottom>0&&rect.top<window.innerHeight&&rect.right>0&&rect.left<window.innerWidth;
                });
                if(!rects.length)continue;
                var layer=document.createElement('div');
                layer.id=layerId;
                layer.style.cssText='position:fixed;inset:0;pointer-events:none;z-index:2147483646;';
                rects.forEach(function(rect){
                  var mark=document.createElement('span');
                  mark.style.cssText='position:absolute;left:'+rect.left+'px;top:'+rect.top+'px;width:'+rect.width+'px;height:'+rect.height+'px;background:'+color+';border-radius:2px;mix-blend-mode:multiply;';
                  layer.appendChild(mark);
                });
                document.documentElement.appendChild(layer);
                return true;
              }
              return false;
            })($target,'$color');
        """.trimIndent()
        scope.launch { runCatching { navigator?.evaluateJavascript(script) } }
    }

    private fun ttsNetworkConfig(): ReaderTtsNetworkConfig =
        ReaderTtsNetworkConfig(
            baseUrl = baseUrl,
            deviceId = deviceId,
            accessToken = accessToken,
            bookId = bookId,
            locator = lastLocatorJson.takeIf { it.length() > 0 }?.toString().orEmpty(),
        )

    private fun registerTtsResumeTarget() {
        val locatorJson = when {
            lastLocatorJson.length() > 0 -> lastLocatorJson.toString()
            pendingTtsResumeLocatorJson.isNotBlank() -> pendingTtsResumeLocatorJson
            else -> store.positionLocatorJson(bookId)
        }
        ReaderTtsSession.setResumeTarget(
            ReaderTtsResumeTarget(
                baseUrl = baseUrl,
                deviceId = deviceId,
                bookId = bookId,
                epubPath = epubPath,
                readingProfile = readingProfile,
                displayVariantsPath = displayVariantsPath,
                locatorJson = locatorJson,
            ),
        )
        ReaderTtsSession.setContinuationProvider(ttsContinuationProvider)
        val lookaheadLocator = runCatching {
            JSONObject(locatorJson).optString("href")
        }.getOrDefault("").ifBlank {
            runCatching { JSONObject(locatorJson).optString("chapterLocator") }.getOrDefault("")
        }
        ReaderTtsSession.setLookaheadProvider(
            ReaderTtsBookLookaheadProvider(
                applicationContext,
                bookId,
                lookaheadLocator,
            ),
        )
    }

    private fun storedAccessToken(): String = runCatching {
        val preferences = getSharedPreferences(CLICK_SHELL_PREFS, Context.MODE_PRIVATE)
        AndroidCredentialStore(preferences).accessToken()
    }.getOrDefault("")

    private fun launchLocalTtsModelImport() {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
            putExtra(Intent.EXTRA_MIME_TYPES, arrayOf("application/zip", "application/octet-stream"))
        }
        runCatching { startActivityForResult(intent, REQUEST_LOCAL_TTS_MODEL) }
            .onFailure { toast("无法打开语音包选择器：${it.compact()}") }
    }

    private fun launchReaderFontImport() {
        val intent = Intent(Intent.ACTION_OPEN_DOCUMENT).apply {
            addCategory(Intent.CATEGORY_OPENABLE)
            type = "*/*"
            putExtra(Intent.EXTRA_MIME_TYPES, arrayOf("font/ttf", "font/otf", "application/x-font-ttf", "application/octet-stream"))
        }
        runCatching { startActivityForResult(intent, REQUEST_READER_FONT) }
            .onFailure { toast("无法打开字体选择器：${it.compact()}") }
    }

    private fun applyImportedFontToCurrentPage() {
        scope.launch {
            val script = withContext(Dispatchers.IO) { fontManager.readiumInjectionScript() } ?: return@launch
            runCatching { navigator?.evaluateJavascript(script) }
        }
    }

    private fun displayVariantNodes(rawHref: String = lastLocatorJson.optString("href")): JSONArray? {
        val resources = displayVariantPayload.optJSONObject("resources") ?: return null
        val target = normalizedTocHref(Uri.decode(rawHref))
        val key = resources.keys().asSequence().firstOrNull { candidate ->
            val normalized = normalizedTocHref(Uri.decode(candidate))
            normalized == target || normalized.endsWith("/$target") || target.endsWith("/$normalized")
        } ?: return null
        return resources.optJSONObject(key)?.optJSONArray("nodes")
    }

    private fun applyChineseScriptDisplay() {
        val mode = when (readerPreferences.chineseScriptMode) {
            ChineseScriptMode.ORIGINAL -> "original"
            ChineseScriptMode.SIMPLIFIED -> "simplified"
            ChineseScriptMode.TRADITIONAL -> "traditional"
        }
        val nodes = displayVariantNodes() ?: if (mode == "original") JSONArray() else return
        val entries = JSONObject.quote(nodes.toString())
        val script = """
            (function(entries,mode){
              var walker=document.createTreeWalker(document.body||document.documentElement,NodeFilter.SHOW_TEXT,null,false);
              var visible=[],node;
              while((node=walker.nextNode())){
                var parent=node.parentElement;
                if(!parent||parent.closest('script,style,noscript,svg'))continue;
                if(!(node.nodeValue||'').trim())continue;
                visible.push(node);
              }
              for(var i=0;i<visible.length;i++){
                if(typeof visible[i].__clickOriginalText==='string')visible[i].nodeValue=visible[i].__clickOriginalText;
              }
              if(mode==='original')return JSON.stringify({mode:mode,changed:0});
              var cursor=0,changed=0;
              for(var e=0;e<entries.length;e++){
                var item=entries[e]||{},source=item.source_text||'';
                var display=(item.display_text||{})[mode]||source;
                if(!source||display===source)continue;
                for(var n=cursor;n<visible.length;n++){
                  var candidate=visible[n];
                  var original=typeof candidate.__clickOriginalText==='string'?candidate.__clickOriginalText:(candidate.nodeValue||'');
                  if(original===source){
                    if(typeof candidate.__clickOriginalText!=='string')candidate.__clickOriginalText=source;
                    candidate.nodeValue=display;
                    cursor=n+1;
                    changed++;
                    break;
                  }
                }
              }
              document.documentElement.setAttribute('data-click-script-mode',mode);
              return JSON.stringify({mode:mode,changed:changed});
            })(JSON.parse($entries),${JSONObject.quote(mode)});
        """.trimIndent()
        scope.launch { runCatching { navigator?.evaluateJavascript(script) } }
    }

    private fun sourceTextForDisplaySnapshot(displayText: String, href: String): String {
        if (readerPreferences.chineseScriptMode == ChineseScriptMode.ORIGINAL || displayText.isBlank()) return displayText
        val mode = if (readerPreferences.chineseScriptMode == ChineseScriptMode.SIMPLIFIED) "simplified" else "traditional"
        val nodes = displayVariantNodes(href) ?: return displayText
        val replacements = mutableListOf<Pair<String, String>>()
        for (index in 0 until nodes.length()) {
            val item = nodes.optJSONObject(index) ?: continue
            val source = item.optString("source_text")
            val shown = item.optJSONObject("display_text")?.optString(mode).orEmpty()
            if (source.isEmpty() || shown.isEmpty() || source.length != shown.length || source == shown) continue
            if (shown == displayText) return source
            val offset = shown.indexOf(displayText)
            if (offset >= 0 && offset + displayText.length <= source.length) {
                return source.substring(offset, offset + displayText.length)
            }
            replacements.add(shown to source)
        }
        var restored = displayText
        replacements.sortedByDescending { it.first.length }.forEach { (shown, source) ->
            restored = restored.replace(shown, source)
        }
        return restored
    }

    private fun ensureTtsNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), REQUEST_NOTIFICATION_PERMISSION)
        }
    }

    private fun enqueueReadiumPosition(locator: Locator) {
        val pageIndex = (lastPageNumber - 1).coerceAtLeast(0)
        val totalPages = lastPageTotal.coerceAtLeast(1)
        val payload = JSONObject()
            .put("book_id", bookId)
            .put("chapter_locator", locator.href.toString())
            .put("page_index", pageIndex)
            .put("total_pages", totalPages)
            .put("page_ratio", pageIndex.toDouble() / totalPages.toDouble())
            .put("locator", locator.toJSON())
        store.saveLocalPositionAndEnqueueLatest(positionOperationId(), bookId, payload)
        ClickSyncScheduler.enqueueMetadata(this)
    }

    override fun onPageChanged(page: Int, total: Int, locator: Locator) {
        val viewportPreviewActive = viewportPreviewAnchor != null
        if (!viewportPreviewActive) {
            lastLocatorJson = locator.toJSON()
            pendingTtsResumeLocatorJson = ""
            ReaderTtsSession.updateResumeLocator(bookId, lastLocatorJson.toString())
        }
        lastPageNumber = (page + 1).coerceAtMost(total.coerceAtLeast(1))
        lastPageTotal = total.coerceAtLeast(1)
        runOnUiThread {
            locator.title?.trim()?.takeIf { it.isNotBlank() }?.let { readerTitle.text = it }
            progressText.text = "$lastPageNumber/$lastPageTotal"
            progressText.visibility = if (readerPreferences.showProgress) View.VISIBLE else View.GONE
        }
        if (viewportPreviewActive) return
        val payload = JSONObject()
            .put("book_id", bookId)
            .put("chapter_locator", locator.href.toString())
            .put("page_index", page)
            .put("total_pages", total.coerceAtLeast(1))
            .put("page_ratio", if (total > 0) page.toDouble() / total.toDouble() else 0.0)
            .put("locator", locator.toJSON())
        store.saveLocalPositionAndEnqueueLatest(positionOperationId(), bookId, payload)
        ClickSyncScheduler.enqueueMetadata(this)
    }

    override fun onPageLoaded() {
        applyImportedFontToCurrentPage()
        mainHandler.postDelayed(::applyChineseScriptDisplay, 120L)
        scope.launch { refreshReadiumHighlights() }
        lastTtsHighlightText = ""
        updateTtsTextHighlight(ReaderTtsSession.current())
    }

    @Deprecated("Android back dispatches through the activity stack on supported versions")
    override fun onBackPressed() {
        if (selectionActionModeActive) {
            selectionActionMode?.finish()
            scope.launch { navigator?.clearSelection() }
            return
        }
        if (chromeVisible) {
            setChromeVisible(false)
            return
        }
        super.onBackPressed()
    }

    override fun shouldJumpToLink(link: Link): Boolean = true

    override fun onTap(point: PointF): Boolean = false

    override fun onDragStart(start: PointF, current: PointF): Boolean = false

    override fun onDragMove(start: PointF, current: PointF): Boolean = false

    override fun onDragEnd(start: PointF, end: PointF): Boolean = false

    override fun onExternalLinkActivated(url: AbsoluteUrl) {
        runCatching {
            startActivity(Intent(Intent.ACTION_VIEW, Uri.parse(url.toString())))
        }
    }

    override fun onResourceLoadFailed(url: Url, error: ReadError) {
        toast("Readium 资源加载失败：$url")
    }

    override fun onJumpToLocator(locator: Locator) {
        lastLocatorJson = locator.toJSON()
        enqueueReadiumPosition(locator)
    }

    private fun request(method: String, rawUrl: String, body: JSONObject?): String {
        check(hasRemoteEndpoint()) { "Mac Reader API 当前不可用" }
        val safeUrl = ReaderApiUrlPolicy.resolve(baseUrl, rawUrl)
        val connection = URL(safeUrl).openConnection() as HttpURLConnection
        connection.instanceFollowRedirects = false
        connection.connectTimeout = 8000
        connection.readTimeout = 180000
        connection.requestMethod = method
        connection.setRequestProperty("Accept", "application/json")
        connection.setRequestProperty("X-Click-Device-Id", deviceId)
        if (accessToken.isNotBlank()) {
            connection.setRequestProperty("X-Click-Access-Token", accessToken)
            connection.setRequestProperty("Authorization", "Bearer $accessToken")
        }
        if (body != null) {
            connection.doOutput = true
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
            val bytes = body.toString().toByteArray(StandardCharsets.UTF_8)
            connection.setFixedLengthStreamingMode(bytes.size)
            connection.outputStream.use { output: OutputStream -> output.write(bytes) }
        }
        val code = connection.responseCode
        val stream = if (code in 200..299) connection.inputStream else connection.errorStream
        val text = stream?.use { it.readBytes().toString(Charsets.UTF_8) }.orEmpty()
        connection.disconnect()
        if (code !in 200..299) error("HTTP $code $text")
        return text
    }

    private fun androidMetadata(action: String, displayTextSnapshot: String = ""): JSONObject =
        JSONObject()
            .put("source", "click_android_readium_navigator")
            .put("readium_toolkit_dependency", READIUM_TOOLKIT_DEPENDENCY)
            .put("device_id", deviceId)
            .put("action", action)
            .put("conflict_policy", "updated_at_device_id_operation_id")
            .put("display_script_mode", readerPreferences.chineseScriptMode.name.lowercase())
            .put("display_text_snapshot", displayTextSnapshot)
            .put("source_locator_preserved", true)

    private fun selectionText(locator: Locator): String =
        locator.text.highlight ?: locator.title ?: locator.href.toString()

    private fun selectionWord(locator: Locator): String =
        selectionText(locator).trim().split(Regex("\\s+")).firstOrNull().orEmpty()

    private fun copyText(text: String) {
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
        clipboard?.setPrimaryClip(ClipData.newPlainText("Click Readium selection", text))
        toast("已复制")
    }

    private fun encode(value: String): String = URLEncoder.encode(value, "UTF-8")

    private fun cleanBaseUrl(value: String): String = value.trim().trimEnd('/')

    private fun hasRemoteEndpoint(): Boolean = baseUrl.isNotBlank()

    private fun hasInternetCapability(): Boolean {
        val manager = getSystemService(ConnectivityManager::class.java) ?: return false
        return try {
            val activeNetwork = manager.activeNetwork ?: return false
            val capabilities = manager.getNetworkCapabilities(activeNetwork) ?: return false
            capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
        } catch (_: RuntimeException) {
            false
        }
    }

    private fun operationId(): String = "android-readium-${UUID.randomUUID()}"

    private fun positionOperationId(): String {
        val safeDeviceId = deviceId.ifBlank { "device" }.replace(Regex("[^A-Za-z0-9._-]"), "_")
        return "android-readium-position-$safeDeviceId-${UUID.randomUUID()}"
    }

    private fun preferenceSeekRow(
        label: String,
        minimum: Int,
        maximum: Int,
        current: Int,
        onStartTracking: (() -> Unit)? = null,
        onPreview: ((Int) -> Unit)? = null,
        onChanged: (Int) -> Unit,
    ): LinearLayout {
        var pendingPreview: Runnable? = null
        var pendingPreviewValue = current.coerceIn(minimum, maximum)
        var lastPreviewAt = 0L
        fun cancelPendingPreview() {
            pendingPreview?.let(mainHandler::removeCallbacks)
            pendingPreview = null
        }
        fun schedulePreview(value: Int) {
            val preview = onPreview ?: return
            pendingPreviewValue = value
            val now = SystemClock.uptimeMillis()
            val elapsed = now - lastPreviewAt
            if (elapsed >= VISUAL_PREVIEW_INTERVAL_MS) {
                cancelPendingPreview()
                lastPreviewAt = now
                preview(value)
                return
            }
            cancelPendingPreview()
            val task = Runnable {
                pendingPreview = null
                lastPreviewAt = SystemClock.uptimeMillis()
                preview(pendingPreviewValue)
            }
            pendingPreview = task
            mainHandler.postDelayed(task, VISUAL_PREVIEW_INTERVAL_MS - elapsed)
        }
        val valueText = TextView(this).apply {
            text = current.coerceIn(minimum, maximum).toString()
            textSize = 13f
            setTextColor(chromeMutedColor())
            gravity = Gravity.END or Gravity.CENTER_VERTICAL
        }
        val seekBar = SeekBar(this).apply {
            max = maximum - minimum
            progress = current.coerceIn(minimum, maximum) - minimum
            val accent = if (readerPreferences.theme == ReaderTheme.NIGHT) {
                Color.rgb(144, 166, 214)
            } else {
                Color.rgb(79, 103, 154)
            }
            val track = if (readerPreferences.theme == ReaderTheme.NIGHT) {
                Color.rgb(75, 78, 72)
            } else {
                Color.rgb(205, 204, 196)
            }
            progressTintList = ColorStateList.valueOf(accent)
            thumbTintList = ColorStateList.valueOf(accent)
            progressBackgroundTintList = ColorStateList.valueOf(track)
            setOnSeekBarChangeListener(object : SeekBar.OnSeekBarChangeListener {
                override fun onProgressChanged(bar: SeekBar?, progress: Int, fromUser: Boolean) {
                    val value = minimum + progress
                    valueText.text = value.toString()
                    if (fromUser) schedulePreview(value)
                }

                override fun onStartTrackingTouch(bar: SeekBar?) {
                    onStartTracking?.invoke()
                }

                override fun onStopTrackingTouch(bar: SeekBar?) {
                    cancelPendingPreview()
                    onChanged(minimum + (bar?.progress ?: 0))
                }
            })
        }
        return LinearLayout(this).apply {
            orientation = LinearLayout.HORIZONTAL
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(2), 0, dp(2))
            addView(
                TextView(this@ClickReadiumNavigatorActivity).apply {
                    text = label
                    textSize = 14f
                    setTextColor(chromeTextColor())
                    gravity = Gravity.CENTER_VERTICAL
                },
                LinearLayout.LayoutParams(dp(52), dp(42)),
            )
            addView(seekBar, LinearLayout.LayoutParams(0, dp(42), 1f))
            addView(valueText, LinearLayout.LayoutParams(dp(42), dp(42)))
        }
    }

    private fun toolbarButton(label: String, action: () -> Boolean): Button =
        button(label, action).apply {
            textSize = 16f
            setTextColor(chromeTextColor())
            minWidth = dp(50)
            minHeight = dp(48)
            background = ClickUi.ripple(
                Color.TRANSPARENT,
                dp(24).toFloat(),
                Color.TRANSPARENT,
                0,
                ClickUi.accent(this@ClickReadiumNavigatorActivity, readerPreferences.theme == ReaderTheme.NIGHT),
            )
        }

    private fun toolbarIconButton(
        iconResource: Int,
        description: String,
        action: () -> Boolean,
    ): Button = button("", action).apply {
        ClickUi.styleIconButton(
            this,
            iconResource,
            description,
            readerPreferences.theme == ReaderTheme.NIGHT,
        )
        minWidth = dp(50)
        minHeight = dp(48)
    }

    private fun bottomButton(label: String, action: () -> Boolean): Button =
        button(label, action).apply {
            textSize = 14f
            setTypeface(android.graphics.Typeface.DEFAULT_BOLD)
            setTextColor(chromeTextColor())
            minWidth = dp(58)
            minHeight = dp(44)
            background = ClickUi.ripple(
                Color.TRANSPARENT,
                dp(22).toFloat(),
                Color.TRANSPARENT,
                0,
                ClickUi.accent(this@ClickReadiumNavigatorActivity, readerPreferences.theme == ReaderTheme.NIGHT),
            )
        }

    private fun bottomIconButton(
        iconResource: Int,
        description: String,
        action: () -> Boolean,
    ): Button = button("", action).apply {
        ClickUi.styleIconButton(
            this,
            iconResource,
            description,
            readerPreferences.theme == ReaderTheme.NIGHT,
        )
        minWidth = dp(58)
        minHeight = dp(44)
    }

    private fun button(label: String, action: () -> Boolean): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            textSize = 12f
            setOnClickListener { action() }
        }

    private fun pillButton(label: String, action: () -> Boolean): Button =
        button(label, action).apply {
            ClickUi.stylePill(this, false, readerPreferences.theme == ReaderTheme.NIGHT)
            minWidth = dp(58)
        }

    private fun primaryButton(label: String, action: () -> Boolean): Button =
        button(label, action).apply {
            ClickUi.stylePrimary(this, readerPreferences.theme == ReaderTheme.NIGHT)
        }

    private fun rounded(color: Int, radius: Int, strokeColor: Int): GradientDrawable =
        GradientDrawable().apply {
            setColor(color)
            cornerRadius = radius.toFloat()
            setStroke(1, strokeColor)
        }

    private fun toast(message: String) {
        runOnUiThread { Toast.makeText(this, message, Toast.LENGTH_LONG).show() }
    }

    private fun Throwable.compact(): String =
        (message ?: javaClass.simpleName).let { if (it.length > 120) it.substring(0, 120) else it }

    private fun dp(value: Int): Int = (value * resources.displayMetrics.density).toInt()

    companion object {
        const val EXTRA_BASE_URL = "base_url"
        const val EXTRA_DEVICE_ID = "device_id"
        const val EXTRA_ACCESS_TOKEN = "access_token"
        const val EXTRA_BOOK_ID = "book_id"
        const val EXTRA_EPUB_PATH = "epub_path"
        const val EXTRA_SHOW_TOC = "show_toc"
        const val EXTRA_READING_PROFILE = "reading_profile"
        const val EXTRA_DISPLAY_VARIANTS_PATH = "display_variants_path"
        const val EXTRA_TTS_RESUME_LOCATOR = "tts_resume_locator"
        const val EXTRA_TARGET_LOCATOR = "target_locator"
        private const val CLICK_SHELL_PREFS = "ClickShellPrefs"
        private const val REQUEST_LOCAL_TTS_MODEL = 3102
        private const val REQUEST_NOTIFICATION_PERMISSION = 3103
        private const val REQUEST_RECORD_AUDIO_PERMISSION = 3104
        private const val REQUEST_READER_FONT = 3105
        private const val SELECTION_MENU_BASE = 4100
        private const val PRIMARY_SELECTION_ACTION_COUNT = 4
        private const val READIUM_FRAGMENT_TAG = "click_readium_epub_navigator"
        private const val PAGE_TURN_SETTLE_MS = 260L
        private const val VISUAL_PREVIEW_INTERVAL_MS = 80L
        private const val VIEWPORT_ANCHOR_PREVIEW_DELAY_MS = 48L
        private const val VIEWPORT_ANCHOR_COMMIT_DELAY_MS = 96L
        private const val VIEWPORT_ANCHOR_SETTLE_MS = 180L
        private const val RED_HIGHLIGHT_DECORATION_GROUP = "click-red-highlights"
        private const val READIUM_TOOLKIT_DEPENDENCY = "org.readium.kotlin-toolkit"
        private val selectionActions = listOf("复制", "标红", "备注", "播放本句", "查词", "语音备注", "问 AI")
    }
}
