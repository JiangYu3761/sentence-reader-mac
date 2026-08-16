package com.click.shell

import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.speech.tts.Voice
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.OutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.nio.charset.StandardCharsets
import java.util.Locale
import java.util.concurrent.CopyOnWriteArraySet
import java.util.concurrent.Executors
import java.util.concurrent.Future

enum class ReaderTtsState {
    IDLE,
    PREPARING,
    PLAYING,
    PAUSED,
    STOPPED,
    ERROR,
}

object ReaderTtsTransitionPolicy {
    fun allows(from: ReaderTtsState, to: ReaderTtsState): Boolean =
        from == to || to in when (from) {
            ReaderTtsState.IDLE -> setOf(ReaderTtsState.PREPARING, ReaderTtsState.ERROR, ReaderTtsState.STOPPED)
            ReaderTtsState.PREPARING -> setOf(ReaderTtsState.PLAYING, ReaderTtsState.ERROR, ReaderTtsState.STOPPED)
            ReaderTtsState.PLAYING -> setOf(ReaderTtsState.PAUSED, ReaderTtsState.PREPARING, ReaderTtsState.ERROR, ReaderTtsState.STOPPED)
            ReaderTtsState.PAUSED -> setOf(ReaderTtsState.PLAYING, ReaderTtsState.PREPARING, ReaderTtsState.ERROR, ReaderTtsState.STOPPED)
            ReaderTtsState.STOPPED -> setOf(ReaderTtsState.PREPARING, ReaderTtsState.ERROR)
            ReaderTtsState.ERROR -> setOf(ReaderTtsState.PREPARING, ReaderTtsState.STOPPED)
        }
}

data class ReaderTtsSnapshot(
    val state: ReaderTtsState = ReaderTtsState.IDLE,
    val engineLabel: String = "本地高质量",
    val currentText: String = "",
    val index: Int = 0,
    val total: Int = 0,
    val ratePercent: Int = 100,
    val timerMinutes: Int = 60,
    val bufferedMinutes: Int = 0,
    val bufferTargetMinutes: Int = ReaderTtsBufferPolicy.LOW_WATER_MINUTES,
    val bufferFilling: Boolean = false,
    val bufferedToBookEnd: Boolean = false,
    val message: String = "",
)

data class ReaderTtsNetworkConfig(
    val baseUrl: String,
    val deviceId: String,
    val accessToken: String,
    val bookId: String,
    val locator: String,
)

data class ReaderTtsResumeTarget(
    val baseUrl: String,
    val deviceId: String,
    val bookId: String,
    val epubPath: String,
    val readingProfile: String,
    val displayVariantsPath: String,
    val locatorJson: String,
)

fun interface ReaderTtsContinuationProvider {
    fun request(direction: Int, onReady: (List<String>) -> Unit)
}

internal object ReaderTtsFallbackPolicy {
    fun useAndroidOfflineVoice(mode: ReaderTtsMode, hasPackagedVoice: Boolean): Boolean =
        !hasPackagedVoice && mode != ReaderTtsMode.MICROSOFT_ONLINE
}

internal enum class ReaderTtsSystemState {
    INITIALIZING,
    READY,
    UNAVAILABLE,
}

internal enum class ReaderTtsSystemAction {
    WAIT,
    SPEAK,
    ERROR,
}

internal object ReaderTtsSystemPolicy {
    private val chinese = Regex("[\\p{IsHan}]")

    fun action(state: ReaderTtsSystemState): ReaderTtsSystemAction = when (state) {
        ReaderTtsSystemState.INITIALIZING -> ReaderTtsSystemAction.WAIT
        ReaderTtsSystemState.READY -> ReaderTtsSystemAction.SPEAK
        ReaderTtsSystemState.UNAVAILABLE -> ReaderTtsSystemAction.ERROR
    }

    fun requiresChineseVoice(text: String): Boolean = chinese.containsMatchIn(text)

    fun chooseLanguage(availableLanguages: List<String>, text: String): String? {
        if (availableLanguages.isEmpty()) return null
        return if (requiresChineseVoice(text)) {
            availableLanguages.firstOrNull { it.equals(Locale.CHINESE.language, ignoreCase = true) }
        } else {
            availableLanguages.first()
        }
    }

    fun shouldResumePending(
        pendingToken: Long?,
        sessionToken: Long,
        state: ReaderTtsSystemState,
    ): Boolean =
        pendingToken != null &&
            pendingToken == sessionToken &&
            action(state) == ReaderTtsSystemAction.SPEAK
}

/** Process-wide playback session retained by the foreground service while the Activity is backgrounded. */
object ReaderTtsSession {
    fun interface Listener {
        fun onChanged(snapshot: ReaderTtsSnapshot)
    }

    private val mainHandler = Handler(Looper.getMainLooper())
    private val executor = Executors.newSingleThreadExecutor()
    private val prefetchExecutor = Executors.newSingleThreadExecutor { work ->
        Thread(
            {
                android.os.Process.setThreadPriority(android.os.Process.THREAD_PRIORITY_BACKGROUND)
                work.run()
            },
            "click-reader-tts-prefetch",
        ).apply { isDaemon = true }
    }
    private val listeners = CopyOnWriteArraySet<Listener>()
    private val temporaryOnlineAudio = CopyOnWriteArraySet<String>()
    private var appContext: Context? = null
    private var modelManager: LocalTtsModelManager? = null
    private var localEngine: SherpaLocalTtsEngine? = null
    private var audioCache: ReaderTtsAudioCache? = null
    private var mediaPlayer: MediaPlayer? = null
    private var mediaWatchdog: Runnable? = null
    private var mediaToken = 0L
    private var mediaSource = ""
    private var mediaEngineLabel = ""
    private var mediaRetryCount = 0
    private var androidTts: TextToSpeech? = null
    private var androidTtsState = ReaderTtsSystemState.INITIALIZING
    private var androidOfflineVoices: List<Voice> = emptyList()
    private var androidTtsVoiceLabel = ""
    private var pendingAndroidTts: PendingAndroidTts? = null
    private var pendingTask: Future<*>? = null
    private var prefetchTask: Future<*>? = null
    private var sessionToken = 0L
    private var bufferSessionToken = 0L
    private var segments: List<String> = emptyList()
    private var index = 0
    private var preferences = ReaderPreferences()
    private var networkConfig = ReaderTtsNetworkConfig("", "", "", "", "")
    private var snapshot = ReaderTtsSnapshot()
    private var stopRunnable: Runnable? = null
    private var activeEngineLabel = "本地高质量 · 私密"
    private var continuationProvider: ReaderTtsContinuationProvider? = null
    private var lookaheadProvider: ReaderTtsLookaheadProvider? = null
    private var continueAcrossPages = false
    private var resumeTarget: ReaderTtsResumeTarget? = null
    private var pendingResumeTarget: ReaderTtsResumeTarget? = null
    private var bufferedDurationMs = 0L
    private var bufferFilling = false
    private var bufferedToBookEnd = false
    private var nextPrefetchRetryAtMs = 0L

    @Synchronized
    fun addListener(listener: Listener) {
        listeners += listener
        listener.onChanged(snapshot)
    }

    @Synchronized
    fun removeListener(listener: Listener) {
        listeners -= listener
    }

    @Synchronized
    fun current(): ReaderTtsSnapshot = snapshot

    /** Returns only the active spoken sentence owned by the requested book. */
    @Synchronized
    fun currentTextForBook(bookId: String): String {
        val active = snapshot.state == ReaderTtsState.PREPARING ||
            snapshot.state == ReaderTtsState.PLAYING ||
            snapshot.state == ReaderTtsState.PAUSED
        if (!active || networkConfig.bookId != bookId) return ""
        return snapshot.currentText.trim()
    }

    @Synchronized
    @JvmOverloads
    fun start(
        context: Context,
        sourceSegments: List<String>,
        prefs: ReaderPreferences,
        network: ReaderTtsNetworkConfig,
        continueAcrossPages: Boolean = false,
    ) {
        initialize(context)
        val cleaned = sourceSegments.flatMap(ReaderTtsTextNormalizer::segment).filter(String::isNotBlank)
        if (cleaned.isEmpty()) {
            publish(ReaderTtsState.ERROR, "没有取得当前页可朗读文字")
            return
        }
        stopPlaybackOnly()
        sessionToken += 1
        bufferSessionToken += 1
        cancelBufferPrefetch()
        bufferedDurationMs = 0L
        bufferFilling = false
        bufferedToBookEnd = false
        nextPrefetchRetryAtMs = 0L
        segments = cleaned
        index = 0
        preferences = prefs
        networkConfig = network
        resumeTarget = pendingResumeTarget?.takeIf { it.bookId == network.bookId }
        pendingResumeTarget = null
        this.continueAcrossPages = continueAcrossPages
        activeEngineLabel = when (prefs.ttsMode) {
            ReaderTtsMode.LOCAL_PRIVATE -> "本地高质量 · 私密"
            ReaderTtsMode.SMART -> "本地优先"
            ReaderTtsMode.MICROSOFT_ONLINE -> "微软在线 · YunjianNeural"
        }
        scheduleStop(prefs.ttsTimerMinutes)
        publish(ReaderTtsState.PREPARING, "正在准备第 1 句")
        ReaderTtsPlaybackService.show(requireNotNull(appContext), snapshot)
        playCurrent(sessionToken)
    }

    @Synchronized
    fun pauseOrResume() {
        when (snapshot.state) {
            ReaderTtsState.PLAYING -> {
                cancelBufferPrefetch()
                cancelMediaWatchdog()
                mediaPlayer?.pause()
                if (mediaPlayer == null) androidTts?.stop()
                publish(ReaderTtsState.PAUSED, "已暂停")
            }
            ReaderTtsState.PAUSED -> {
                val player = mediaPlayer
                if (player != null) {
                    player.start()
                    scheduleMediaWatchdog(player, mediaToken, mediaSource, mediaEngineLabel, mediaRetryCount)
                    publish(ReaderTtsState.PLAYING, "继续朗读")
                    maintainAheadBuffer()
                } else {
                    sessionToken += 1
                    publish(ReaderTtsState.PREPARING, "正在继续")
                    playCurrent(sessionToken)
                }
            }
            else -> Unit
        }
    }

    @Synchronized
    fun next() {
        if (segments.isEmpty()) return
        if (index >= segments.lastIndex && continueAcrossPages && continuationProvider != null) {
            requestAdjacentPage(1, "正在进入下一页")
            return
        }
        index = (index + 1).coerceAtMost(segments.lastIndex)
        restartCurrent("下一句")
    }

    @Synchronized
    fun previous() {
        if (segments.isEmpty()) return
        if (index <= 0 && continueAcrossPages && continuationProvider != null) {
            requestAdjacentPage(-1, "正在进入上一页")
            return
        }
        index = (index - 1).coerceAtLeast(0)
        restartCurrent("上一句")
    }

    @Synchronized
    fun setRate(ratePercent: Int) {
        preferences = preferences.copy(ttsRatePercent = ratePercent.coerceIn(80, 150))
        if (segments.isNotEmpty()) restartCurrent("语速 ${preferences.ttsRatePercent}%")
    }

    @Synchronized
    fun setTimer(minutes: Int) {
        val safe = minutes.takeIf { it == 30 || it == 60 || it == 90 } ?: 60
        preferences = preferences.copy(ttsTimerMinutes = safe)
        scheduleStop(safe)
        publish(snapshot.state, "${safe} 分钟后停止")
    }

    @Synchronized
    internal fun setTimerForDebug(delayMs: Long) {
        check(BuildConfig.DEBUG) { "debug timer is unavailable in release builds" }
        scheduleStopAfter(delayMs.coerceIn(250L, 60_000L))
        publish(snapshot.state, "Debug 倒计时已启动")
    }

    @Synchronized
    fun stop() {
        sessionToken += 1
        bufferSessionToken += 1
        cancelBufferPrefetch()
        stopPlaybackOnly()
        stopRunnable?.let(mainHandler::removeCallbacks)
        stopRunnable = null
        segments = emptyList()
        index = 0
        continueAcrossPages = false
        bufferedDurationMs = 0L
        bufferFilling = false
        bufferedToBookEnd = false
        publish(ReaderTtsState.STOPPED, "朗读已停止")
        appContext?.let(ReaderTtsPlaybackService::stop)
    }

    @Synchronized
    fun unloadLocalModel() {
        stop()
        localEngine?.release()
    }

    @Synchronized
    fun setContinuationProvider(provider: ReaderTtsContinuationProvider?) {
        continuationProvider = provider
    }

    @Synchronized
    fun setLookaheadProvider(provider: ReaderTtsLookaheadProvider?) {
        lookaheadProvider = provider
    }

    @Synchronized
    fun clearContinuationProvider(provider: ReaderTtsContinuationProvider) {
        if (continuationProvider === provider) continuationProvider = null
    }

    @Synchronized
    fun setResumeTarget(target: ReaderTtsResumeTarget) {
        pendingResumeTarget = target
    }

    @Synchronized
    fun updateResumeLocator(bookId: String, locatorJson: String) {
        if (bookId.isBlank() || locatorJson.isBlank()) return
        val target = resumeTarget ?: return
        if (target.bookId != bookId) return
        resumeTarget = target.copy(locatorJson = locatorJson)
        if (networkConfig.bookId == bookId) {
            networkConfig = networkConfig.copy(locator = locatorJson)
        }
    }

    @Synchronized
    fun readerIntent(context: Context): Intent? {
        val target = resumeTarget ?: return null
        return Intent(context, ClickReadiumNavigatorActivity::class.java).apply {
            putExtra(ClickReadiumNavigatorActivity.EXTRA_BASE_URL, target.baseUrl)
            putExtra(ClickReadiumNavigatorActivity.EXTRA_DEVICE_ID, target.deviceId)
            putExtra(ClickReadiumNavigatorActivity.EXTRA_BOOK_ID, target.bookId)
            putExtra(ClickReadiumNavigatorActivity.EXTRA_EPUB_PATH, target.epubPath)
            putExtra(ClickReadiumNavigatorActivity.EXTRA_READING_PROFILE, target.readingProfile)
            putExtra(ClickReadiumNavigatorActivity.EXTRA_DISPLAY_VARIANTS_PATH, target.displayVariantsPath)
            putExtra(ClickReadiumNavigatorActivity.EXTRA_TTS_RESUME_LOCATOR, target.locatorJson)
            addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP)
        }
    }

    private fun initialize(context: Context) {
        if (appContext != null) return
        appContext = context.applicationContext
        modelManager = LocalTtsModelManager(context)
        localEngine = SherpaLocalTtsEngine(context)
        audioCache = ReaderTtsAudioCache(context)
        androidTtsState = ReaderTtsSystemState.INITIALIZING
        androidTts = TextToSpeech(context.applicationContext) { status ->
            val engine = androidTts
            val offlineVoices = if (status == TextToSpeech.SUCCESS) {
                engine?.voices.orEmpty()
                    .filter { !it.isNetworkConnectionRequired }
                    .sortedByDescending { it.locale.language == Locale.CHINESE.language }
            } else {
                emptyList()
            }
            mainHandler.post {
                acceptAndroidTtsInitialization(offlineVoices)
            }
        }.apply {
            setOnUtteranceProgressListener(object : UtteranceProgressListener() {
                override fun onStart(utteranceId: String?) {
                    mainHandler.post { publish(ReaderTtsState.PLAYING, "Android 系统语音") }
                }

                override fun onDone(utteranceId: String?) {
                    mainHandler.post { advanceAfterCompletion() }
                }

                @Deprecated("Deprecated by Android")
                override fun onError(utteranceId: String?) {
                    mainHandler.post { publish(ReaderTtsState.ERROR, "Android 系统语音播放失败") }
                }
            })
        }
    }

    @Synchronized
    private fun restartCurrent(reason: String) {
        sessionToken += 1
        stopPlaybackOnly()
        publish(ReaderTtsState.PREPARING, reason)
        playCurrent(sessionToken)
    }

    @Synchronized
    private fun playCurrent(token: Long) {
        val text = segments.getOrNull(index) ?: run {
            stop()
            return
        }
        publish(ReaderTtsState.PREPARING, "正在准备 ${index + 1}/${segments.size}")
        when (preferences.ttsMode) {
            ReaderTtsMode.LOCAL_PRIVATE,
            ReaderTtsMode.SMART,
            -> playLocal(token, text)
            ReaderTtsMode.MICROSOFT_ONLINE -> playMicrosoftOnline(token, text)
        }
    }

    private fun playLocal(token: Long, text: String) {
        val voice = modelManager?.selectedVoice(preferences.ttsLocalModelId, preferences.ttsLocalVoiceId)
        if (voice == null) {
            playAndroidSystem(token, text)
            return
        }
        activeEngineLabel = "本地 Kokoro · ${voice.displayName}"
        pendingTask = executor.submit {
            runCatching {
                requireNotNull(localEngine).synthesize(text, voice, preferences.ttsRatePercent)
            }.onSuccess { file ->
                mainHandler.post {
                    if (token == sessionToken) playMedia(token, file.absolutePath, "本地 Kokoro · ${voice.displayName}")
                }
            }.onFailure { error ->
                mainHandler.post {
                    if (token != sessionToken) return@post
                    playAndroidSystem(token, text)
                }
            }
        }
    }

    private fun playMicrosoftOnline(token: Long, text: String) {
        activeEngineLabel = "微软在线 · zh-CN-YunjianNeural"
        if (networkConfig.baseUrl.isBlank()) {
            publish(ReaderTtsState.ERROR, "微软在线语音需要可访问的 Mac Reader API")
            return
        }
        pendingTask = executor.submit {
            runCatching { requestOnlineAudio(text) }
                .onSuccess { audioUrl ->
                    mainHandler.post {
                        if (token == sessionToken) {
                            playMedia(token, audioUrl, "微软在线 · zh-CN-YunjianNeural")
                        } else {
                            deleteTemporaryOnlineAudio(audioUrl)
                        }
                    }
                }
                .onFailure { error ->
                    mainHandler.post {
                        if (token == sessionToken) publish(ReaderTtsState.ERROR, "微软在线语音失败：${error.compact()}")
                    }
                }
        }
    }

    @Synchronized
    private fun playMedia(token: Long, source: String, engineLabel: String, retryCount: Int = 0) {
        stopPlaybackOnly()
        if (isTemporaryOnlineAudio(source)) temporaryOnlineAudio += source
        activeEngineLabel = engineLabel
        mediaToken = token
        mediaSource = source
        mediaEngineLabel = engineLabel
        mediaRetryCount = retryCount
        mediaPlayer = MediaPlayer().apply {
            setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            setWakeMode(requireNotNull(appContext), PowerManager.PARTIAL_WAKE_LOCK)
            if (source.startsWith("http://") || source.startsWith("https://")) {
                val headers = mutableMapOf("X-Click-Device-Id" to networkConfig.deviceId)
                if (networkConfig.accessToken.isNotBlank()) {
                    headers["X-Click-Access-Token"] = networkConfig.accessToken
                    headers["Authorization"] = "Bearer ${networkConfig.accessToken}"
                }
                setDataSource(requireNotNull(appContext), Uri.parse(source), headers)
            } else {
                setDataSource(source)
            }
            setOnPreparedListener { player ->
                if (token != sessionToken) {
                    if (mediaPlayer === player) mediaPlayer = null
                    player.release()
                    return@setOnPreparedListener
                }
                player.start()
                scheduleMediaWatchdog(player, token, source, engineLabel, retryCount)
                publish(ReaderTtsState.PLAYING, engineLabel)
                prefetchNextLocal()
                maintainAheadBuffer()
            }
            setOnCompletionListener {
                releaseMediaPlayer()
                deleteTemporaryOnlineAudio(source)
                advanceAfterCompletion()
            }
            setOnErrorListener { _, _, _ ->
                recoverMediaPlayback(token, source, engineLabel, retryCount)
                true
            }
            prepareAsync()
        }
    }

    @Synchronized
    private fun playAndroidSystem(token: Long, text: String) {
        activeEngineLabel = "Android 本地系统语音${androidTtsVoiceLabel.takeIf(String::isNotBlank)?.let { " · $it" }.orEmpty()}"
        when (ReaderTtsSystemPolicy.action(androidTtsState)) {
            ReaderTtsSystemAction.WAIT -> {
                pendingAndroidTts = PendingAndroidTts(token, text)
                publish(ReaderTtsState.PREPARING, "正在启动 Android 离线系统语音")
                return
            }
            ReaderTtsSystemAction.ERROR -> {
                publish(ReaderTtsState.ERROR, "没有已安装的 Android 离线语音；请先在系统文字转语音设置中下载离线语音")
                return
            }
            ReaderTtsSystemAction.SPEAK -> Unit
        }
        val selectedLanguage = ReaderTtsSystemPolicy.chooseLanguage(
            androidOfflineVoices.map { it.locale.language },
            text,
        )
        val selectedVoice = selectedLanguage?.let { language ->
            androidOfflineVoices.firstOrNull { it.locale.language.equals(language, ignoreCase = true) }
        }
        if (selectedVoice == null) {
            val message = if (ReaderTtsSystemPolicy.requiresChineseVoice(text)) {
                "没有已安装的中文 Android 离线语音；请先在系统文字转语音设置中下载中文离线语音"
            } else {
                "没有适合当前正文的 Android 离线语音"
            }
            publish(ReaderTtsState.ERROR, message)
            return
        }
        pendingAndroidTts = null
        androidTts?.voice = selectedVoice
        androidTtsVoiceLabel = selectedVoice.name
        activeEngineLabel = "Android 本地系统语音 · ${selectedVoice.name}"
        androidTts?.setSpeechRate(preferences.ttsRatePercent / 100f)
        val result = androidTts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "click-tts-$token")
        if (result == TextToSpeech.ERROR) publish(ReaderTtsState.ERROR, "Android 系统语音启动失败")
        else publish(ReaderTtsState.PREPARING, "Android 离线系统语音")
    }

    @Synchronized
    private fun acceptAndroidTtsInitialization(voices: List<Voice>) {
        androidOfflineVoices = voices
        androidTtsVoiceLabel = ""
        androidTtsState = if (voices.isEmpty()) {
            ReaderTtsSystemState.UNAVAILABLE
        } else {
            ReaderTtsSystemState.READY
        }
        val pending = pendingAndroidTts
        pendingAndroidTts = null
        if (ReaderTtsSystemPolicy.shouldResumePending(
                pending?.token,
                sessionToken,
                androidTtsState,
            )
        ) {
            checkNotNull(pending)
            playAndroidSystem(pending.token, pending.text)
        } else if (pending != null && pending.token == sessionToken) {
            publish(
                ReaderTtsState.ERROR,
                "没有已安装的 Android 离线语音；请先在系统文字转语音设置中下载离线语音",
            )
        }
    }

    @Synchronized
    private fun advanceAfterCompletion() {
        if (index >= segments.lastIndex) {
            if (continueAcrossPages && continuationProvider != null) {
                requestAdjacentPage(1, "正在进入下一页")
            } else {
                finishCompleted()
            }
            return
        }
        index += 1
        sessionToken += 1
        playCurrent(sessionToken)
    }

    @Synchronized
    private fun requestAdjacentPage(direction: Int, message: String) {
        val provider = continuationProvider ?: return
        sessionToken += 1
        val expectedToken = sessionToken
        stopPlaybackOnly()
        publish(ReaderTtsState.PREPARING, message)
        provider.request(direction) { adjacent ->
            acceptContinuation(expectedToken, direction, adjacent)
        }
    }

    private fun acceptContinuation(expectedToken: Long, direction: Int, sourceSegments: List<String>) {
        val cleaned = sourceSegments.flatMap(ReaderTtsTextNormalizer::segment).filter(String::isNotBlank)
        mainHandler.post {
            acceptContinuationOnMain(expectedToken, direction, cleaned)
        }
    }

    @Synchronized
    private fun acceptContinuationOnMain(expectedToken: Long, direction: Int, cleaned: List<String>) {
        if (expectedToken != sessionToken) return
        val adjacentPage = if (direction > 0) {
            val previousTail = segments.takeLast(3).toSet()
            cleaned.dropWhile { it in previousTail }
        } else {
            val currentHead = segments.take(3).toSet()
            cleaned.dropLastWhile { it in currentHead }
        }
        if (adjacentPage.isEmpty()) {
            if (direction > 0) {
                finishCompleted()
            } else {
                publish(ReaderTtsState.PREPARING, "已经到开头")
                playCurrent(sessionToken)
            }
            return
        }
        segments = adjacentPage
        index = if (direction > 0) 0 else adjacentPage.lastIndex
        val pageLabel = if (direction > 0) "下一页" else "上一页"
        publish(ReaderTtsState.PREPARING, "$pageLabel · ${index + 1}/${adjacentPage.size}")
        playCurrent(sessionToken)
    }

    @Synchronized
    private fun finishCompleted() {
        sessionToken += 1
        bufferSessionToken += 1
        cancelBufferPrefetch()
        stopPlaybackOnly()
        publish(ReaderTtsState.STOPPED, "朗读已完成")
        segments = emptyList()
        index = 0
        continueAcrossPages = false
        bufferedDurationMs = 0L
        bufferFilling = false
        bufferedToBookEnd = false
        appContext?.let(ReaderTtsPlaybackService::stop)
    }

    private fun prefetchNextLocal() {
        if (preferences.ttsMode == ReaderTtsMode.MICROSOFT_ONLINE) return
        val nextText = segments.getOrNull(index + 1) ?: return
        val voice = modelManager?.selectedVoice(preferences.ttsLocalModelId, preferences.ttsLocalVoiceId) ?: return
        executor.submit { runCatching { localEngine?.synthesize(nextText, voice, preferences.ttsRatePercent) } }
    }

    @Synchronized
    private fun maintainAheadBuffer() {
        if (preferences.ttsMode != ReaderTtsMode.MICROSOFT_ONLINE) return
        if (!continueAcrossPages) return
        if (snapshot.state != ReaderTtsState.PLAYING) return
        if (networkConfig.baseUrl.isBlank()) return
        if (System.currentTimeMillis() < nextPrefetchRetryAtMs) return
        if (prefetchTask?.isDone == false) return
        val provider = lookaheadProvider ?: return
        val cache = audioCache ?: return
        val session = bufferSessionToken
        val current = segments.getOrNull(index).orEmpty()
        val network = networkConfig
        val rate = preferences.ttsRatePercent
        bufferFilling = true
        publishBufferSnapshot()
        prefetchTask = prefetchExecutor.submit {
            var contiguousDuration = 0L
            var finalReachedBookEnd = false
            runCatching {
                val plan = provider.load(
                    current,
                    ReaderTtsBufferPolicy.HIGH_WATER_MS * 2L,
                    rate,
                )
                val items = plan.segments
                if (items.isEmpty()) {
                    finalReachedBookEnd = plan.reachedBookEnd
                    return@runCatching
                }
                var firstMissing = items.size
                var scannedAll = true
                for ((position, item) in items.withIndex()) {
                    if (!bufferSessionIsCurrent(session)) return@runCatching
                    val key = cache.stableKey(
                        network.bookId,
                        MICROSOFT_ENGINE,
                        MICROSOFT_VOICE,
                        rate,
                        item.text,
                    )
                    val cached = cache.find(key)
                    if (cached == null) {
                        firstMissing = position
                        scannedAll = false
                        break
                    }
                    contiguousDuration += cached.durationMs
                    if (contiguousDuration >= ReaderTtsBufferPolicy.HIGH_WATER_MS) {
                        firstMissing = position + 1
                        scannedAll = position == items.lastIndex
                        break
                    }
                }
                val initialDecision = ReaderTtsBufferPolicy.decide(
                    contiguousDuration,
                    plan.reachedBookEnd && scannedAll,
                )
                if (!initialDecision.shouldRefill) {
                    finalReachedBookEnd = plan.reachedBookEnd && scannedAll
                    postBufferSnapshot(session, contiguousDuration, false, finalReachedBookEnd)
                    return@runCatching
                }

                for (position in firstMissing until items.size) {
                    if (!bufferSessionIsCurrent(session)) return@runCatching
                    val item = items[position]
                    val itemNetwork = network.copy(locator = item.chapterLocator)
                    val cached = requestOnlineAudio(item.text, itemNetwork, rate)
                    contiguousDuration += cached.durationMs
                    postBufferSnapshot(session, contiguousDuration, true, false)
                    if (contiguousDuration >= ReaderTtsBufferPolicy.HIGH_WATER_MS) {
                        finalReachedBookEnd = false
                        break
                    }
                    finalReachedBookEnd = plan.reachedBookEnd && position == items.lastIndex
                }
            }.onFailure {
                if (bufferSessionIsCurrent(session)) {
                    nextPrefetchRetryAtMs = System.currentTimeMillis() + PREFETCH_RETRY_COOLDOWN_MS
                }
            }
            postBufferSnapshot(session, contiguousDuration, false, finalReachedBookEnd)
        }
    }

    @Synchronized
    private fun bufferSessionIsCurrent(expected: Long): Boolean =
        expected == bufferSessionToken &&
            preferences.ttsMode == ReaderTtsMode.MICROSOFT_ONLINE &&
            snapshot.state != ReaderTtsState.STOPPED &&
            snapshot.state != ReaderTtsState.ERROR

    private fun postBufferSnapshot(
        session: Long,
        durationMs: Long,
        filling: Boolean,
        reachedBookEnd: Boolean,
    ) {
        mainHandler.post {
            synchronized(this) {
                if (!bufferSessionIsCurrent(session)) return@synchronized
                bufferedDurationMs = durationMs.coerceAtLeast(0L)
                bufferFilling = filling
                bufferedToBookEnd = reachedBookEnd
                publishBufferSnapshot()
            }
        }
    }

    @Synchronized
    private fun publishBufferSnapshot() {
        snapshot = snapshot.copy(
            bufferedMinutes = (bufferedDurationMs / 60_000L).toInt(),
            bufferTargetMinutes = ReaderTtsBufferPolicy.LOW_WATER_MINUTES,
            bufferFilling = bufferFilling,
            bufferedToBookEnd = bufferedToBookEnd,
        )
        listeners.forEach { listener -> runCatching { listener.onChanged(snapshot) } }
        appContext?.let { context ->
            if (snapshot.state != ReaderTtsState.IDLE && snapshot.state != ReaderTtsState.STOPPED) {
                ReaderTtsPlaybackService.show(context, snapshot)
            }
        }
    }

    @Synchronized
    private fun cancelBufferPrefetch() {
        prefetchTask?.cancel(true)
        prefetchTask = null
        bufferFilling = false
    }

    private fun requestOnlineAudio(text: String): String {
        return requestOnlineAudio(text, networkConfig, preferences.ttsRatePercent).file.absolutePath
    }

    private fun requestOnlineAudio(
        text: String,
        network: ReaderTtsNetworkConfig,
        ratePercent: Int,
    ): ReaderTtsCachedAudio {
        val cache = requireNotNull(audioCache)
        val voice = MICROSOFT_VOICE
        val key = cache.stableKey(
            network.bookId,
            MICROSOFT_ENGINE,
            voice,
            ratePercent,
            text,
        )
        cache.find(key)?.let { return it }
        val base = network.baseUrl.trimEnd('/')
        check(base.isNotBlank()) { "微软在线语音需要可访问的 Reader API" }
        val locator = network.locator.trim().let { raw ->
            if (raw.isBlank()) JSONObject() else runCatching { JSONObject(raw) }.getOrDefault(JSONObject())
        }
        val payload = JSONObject()
            .put("text", text)
            .put("kind", "reader_sentence")
            .put("voice", voice)
            .put("book_id", network.bookId)
            .put("locator", locator)
        val endpoint = ReaderApiUrlPolicy.resolve(base, "/v1/android/tts")
        val connection = URL(endpoint).openConnection() as HttpURLConnection
        connection.instanceFollowRedirects = false
        connection.connectTimeout = 8_000
        connection.readTimeout = 90_000
        connection.requestMethod = "POST"
        connection.doOutput = true
        connection.setRequestProperty("Content-Type", "application/json; charset=utf-8")
        connection.setRequestProperty("X-Click-Device-Id", network.deviceId)
        if (network.accessToken.isNotBlank()) {
            connection.setRequestProperty("X-Click-Access-Token", network.accessToken)
            connection.setRequestProperty("Authorization", "Bearer ${network.accessToken}")
        }
        val bytes = payload.toString().toByteArray(StandardCharsets.UTF_8)
        connection.setFixedLengthStreamingMode(bytes.size)
        connection.outputStream.use { output: OutputStream -> output.write(bytes) }
        val code = connection.responseCode
        val body = (if (code in 200..299) connection.inputStream else connection.errorStream)
            ?.use { it.readBytes().toString(Charsets.UTF_8) }
            .orEmpty()
        connection.disconnect()
        check(code in 200..299) { "HTTP $code" }
        val path = JSONObject(body).optString("audio_url")
        check(path.isNotBlank()) { "服务没有返回音频" }
        return downloadOnlineAudio(
            ReaderApiUrlPolicy.resolve(base, path),
            key,
            network,
            voice,
            ratePercent,
        )
    }

    private fun downloadOnlineAudio(
        rawUrl: String,
        cacheKey: String,
        network: ReaderTtsNetworkConfig,
        voice: String,
        ratePercent: Int,
    ): ReaderTtsCachedAudio {
        val cache = requireNotNull(audioCache)
        cache.find(cacheKey)?.let { return it }
        val safeUrl = ReaderApiUrlPolicy.resolve(network.baseUrl, rawUrl)
        val target = cache.newPartial(cacheKey)
        val connection = URL(safeUrl).openConnection() as HttpURLConnection
        connection.instanceFollowRedirects = false
        connection.connectTimeout = 8_000
        connection.readTimeout = 90_000
        connection.requestMethod = "GET"
        connection.setRequestProperty("X-Click-Device-Id", network.deviceId)
        if (network.accessToken.isNotBlank()) {
            connection.setRequestProperty("X-Click-Access-Token", network.accessToken)
            connection.setRequestProperty("Authorization", "Bearer ${network.accessToken}")
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
            return cache.commit(
                cacheKey,
                target,
                network.bookId,
                network.locator,
                voice,
                ratePercent,
            )
        } catch (error: Throwable) {
            target.delete()
            throw error
        } finally {
            connection.disconnect()
        }
    }

    @Synchronized
    private fun scheduleStop(minutes: Int) {
        val safe = minutes.takeIf { it == 30 || it == 60 || it == 90 } ?: 60
        scheduleStopAfter(safe * 60L * 1_000L)
    }

    @Synchronized
    private fun scheduleStopAfter(delayMs: Long) {
        stopRunnable?.let(mainHandler::removeCallbacks)
        stopRunnable = Runnable { stop() }.also {
            mainHandler.postDelayed(it, delayMs.coerceAtLeast(1L))
        }
    }

    @Synchronized
    private fun stopPlaybackOnly() {
        pendingTask?.cancel(true)
        pendingTask = null
        pendingAndroidTts = null
        releaseMediaPlayer()
        cleanupTemporaryOnlineAudio()
        androidTts?.stop()
    }

    @Synchronized
    private fun releaseMediaPlayer() {
        cancelMediaWatchdog()
        val player = mediaPlayer
        mediaPlayer = null
        mediaToken = 0L
        mediaSource = ""
        mediaEngineLabel = ""
        mediaRetryCount = 0
        if (player != null) {
            runCatching { if (player.isPlaying) player.stop() }
            runCatching { player.release() }
        }
    }

    @Synchronized
    private fun recoverMediaPlayback(token: Long, source: String, engineLabel: String, retryCount: Int) {
        releaseMediaPlayer()
        if (token == sessionToken && retryCount < MAX_MEDIA_RETRIES) {
            publish(ReaderTtsState.PREPARING, "正在恢复播放")
            mainHandler.postDelayed(
                { if (token == sessionToken) playMedia(token, source, engineLabel, retryCount + 1) },
                MEDIA_RETRY_DELAY_MS,
            )
        } else if (token == sessionToken) {
            deleteTemporaryOnlineAudio(source)
            publish(ReaderTtsState.ERROR, "$engineLabel 播放失败")
        }
    }

    private fun isTemporaryOnlineAudio(path: String): Boolean {
        val context = appContext ?: return false
        val file = File(path)
        return file.name.startsWith("click-reader-online-tts-") && file.parentFile == context.cacheDir
    }

    private fun deleteTemporaryOnlineAudio(path: String) {
        if (!isTemporaryOnlineAudio(path)) return
        temporaryOnlineAudio -= path
        runCatching { File(path).delete() }
    }

    private fun cleanupTemporaryOnlineAudio() {
        temporaryOnlineAudio.toList().forEach(::deleteTemporaryOnlineAudio)
    }

    private data class PendingAndroidTts(
        val token: Long,
        val text: String,
    )

    @Synchronized
    private fun scheduleMediaWatchdog(
        player: MediaPlayer,
        token: Long,
        source: String,
        engineLabel: String,
        retryCount: Int,
    ) {
        cancelMediaWatchdog()
        val remaining = runCatching { player.duration - player.currentPosition }
            .getOrDefault(MIN_MEDIA_WATCHDOG_MS.toInt())
            .coerceAtLeast(MIN_MEDIA_WATCHDOG_MS.toInt())
            .toLong()
        mediaWatchdog = Runnable {
            if (mediaPlayer === player && token == sessionToken) {
                recoverMediaPlayback(token, source, engineLabel, retryCount)
            }
        }.also { mainHandler.postDelayed(it, remaining + MEDIA_WATCHDOG_GRACE_MS) }
    }

    @Synchronized
    private fun cancelMediaWatchdog() {
        mediaWatchdog?.let(mainHandler::removeCallbacks)
        mediaWatchdog = null
    }

    @Synchronized
    private fun publish(state: ReaderTtsState, message: String) {
        if (!ReaderTtsTransitionPolicy.allows(snapshot.state, state)) return
        snapshot = ReaderTtsSnapshot(
            state = state,
            engineLabel = activeEngineLabel,
            currentText = segments.getOrNull(index).orEmpty(),
            index = index,
            total = segments.size,
            ratePercent = preferences.ttsRatePercent,
            timerMinutes = preferences.ttsTimerMinutes,
            bufferedMinutes = (bufferedDurationMs / 60_000L).toInt(),
            bufferTargetMinutes = ReaderTtsBufferPolicy.LOW_WATER_MINUTES,
            bufferFilling = bufferFilling,
            bufferedToBookEnd = bufferedToBookEnd,
            message = message,
        )
        listeners.forEach { listener -> runCatching { listener.onChanged(snapshot) } }
        appContext?.let { context ->
            if (state != ReaderTtsState.IDLE && state != ReaderTtsState.STOPPED) {
                ReaderTtsPlaybackService.show(context, snapshot)
            }
        }
    }

    private fun Throwable.compact(): String =
        (message ?: javaClass.simpleName).let { if (it.length > 120) it.take(120) else it }

    private const val MAX_MEDIA_RETRIES = 1
    private const val MEDIA_RETRY_DELAY_MS = 250L
    private const val MIN_MEDIA_WATCHDOG_MS = 1_000L
    private const val MEDIA_WATCHDOG_GRACE_MS = 15_000L
    private const val PREFETCH_RETRY_COOLDOWN_MS = 60_000L
    private const val MICROSOFT_ENGINE = "edge-tts"
    private const val MICROSOFT_VOICE = "zh-CN-YunjianNeural"
}
