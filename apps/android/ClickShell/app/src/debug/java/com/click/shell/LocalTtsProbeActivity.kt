package com.click.shell

import android.app.Activity
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.net.Uri
import android.os.Bundle
import android.os.Debug
import android.os.SystemClock
import android.util.Log
import android.widget.TextView
import org.json.JSONObject
import java.io.File
import java.io.RandomAccessFile
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicReference
import kotlin.concurrent.thread

/** Debug-only, shell-permission-protected probe for real offline model import and synthesis. */
class LocalTtsProbeActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val status = TextView(this).apply {
            text = "Click local TTS dynamic probe is running"
            textSize = 18f
            setPadding(24, 24, 24, 24)
        }
        setContentView(status)

        thread(name = "click-local-tts-probe") {
            val result = runProbe()
            File(filesDir, RESULT_FILE).writeText(result.toString(2), Charsets.UTF_8)
            Log.i(LOG_TAG, result.toString())
            runOnUiThread { status.text = if (result.optBoolean("ok")) "PASS" else "FAIL\n${result.optString("error")}" }
        }
    }

    private fun runProbe(): JSONObject {
        val result = JSONObject()
            .put("schema", "click.android.local_tts.dynamic_probe.v1")
            .put("network_required", false)
            .put("archive_expected_sha256", LocalTtsModelManager.KOKORO_ARCHIVE_SHA256)
        return runCatching {
            val archive = File(intent.getStringExtra(EXTRA_ARCHIVE_PATH) ?: File(filesDir, DEFAULT_ARCHIVE).absolutePath)
            require(archive.isFile) { "probe archive is missing: ${archive.absolutePath}" }
            val archiveSha = sha256(archive)
            require(archiveSha == LocalTtsModelManager.KOKORO_ARCHIVE_SHA256) { "probe archive SHA256 mismatch" }

            val manager = LocalTtsModelManager(this)
            val importStarted = SystemClock.elapsedRealtime()
            val imported = manager.importSupportedPackage(Uri.fromFile(archive))
            val importMs = SystemClock.elapsedRealtime() - importStarted
            archive.delete()

            val voice = imported.voices.firstOrNull { it.voiceId == DEFAULT_VOICE_ID }
                ?: imported.voices.first()
            val heapBefore = Debug.getNativeHeapAllocatedSize()
            val synthStarted = SystemClock.elapsedRealtime()
            val generated = SherpaLocalTtsEngine(this).useEngine { engine ->
                engine.synthesize(PROBE_TEXT, voice, 100)
            }
            val synthMs = SystemClock.elapsedRealtime() - synthStarted
            val output = File(filesDir, OUTPUT_WAV)
            generated.copyTo(output, overwrite = true)
            val duration = wavDurationSeconds(output)
            require(duration > 0.1) { "generated WAV duration is invalid" }
            val playbackCompleted = playAndWait(output, duration)
            require(playbackCompleted) { "MediaPlayer did not complete local WAV playback" }
            val microphoneBytes = recordMicrophoneProbe()
            require(microphoneBytes > 512L) { "MediaRecorder did not create a usable local audio note" }
            val stability = runStabilityProbe(
                intent.getIntExtra(EXTRA_STABILITY_SECONDS, 0).coerceIn(0, 900),
                voice,
            )
            val foregroundControls = if (intent.getBooleanExtra(EXTRA_FOREGROUND_SERVICE, true)) {
                runForegroundControlProbe(imported.modelId, voice.voiceId)
            } else {
                JSONObject().put("started", false).put("controls_verified", false)
            }

            result
                .put("ok", true)
                .put("airplane_mode_expected", true)
                .put("model_id", imported.modelId)
                .put("voice_id", voice.voiceId)
                .put("archive_sha256", archiveSha)
                .put("import_ms", importMs)
                .put("synthesis_ms", synthMs)
                .put("audio_bytes", output.length())
                .put("audio_duration_seconds", duration)
                .put("rtf", synthMs / 1000.0 / duration)
                .put("media_player_completed", playbackCompleted)
                .put("microphone_probe_bytes", microphoneBytes)
                .put("stability", stability)
                .put("foreground_service_started", foregroundControls.optBoolean("started"))
                .put("foreground_controls", foregroundControls)
                .put("native_heap_before", heapBefore)
                .put("native_heap_after", Debug.getNativeHeapAllocatedSize())
                .put("output_sha256", sha256(output))
                .put("output_file", OUTPUT_WAV)
        }.getOrElse { error ->
            result
                .put("ok", false)
                .put("error", "${error.javaClass.simpleName}: ${error.message}")
        }
    }

    private fun wavDurationSeconds(file: File): Double {
        RandomAccessFile(file, "r").use { wav ->
            require(wav.length() >= 44) { "WAV is too small" }
            val header = ByteArray(44)
            wav.readFully(header)
            val buffer = ByteBuffer.wrap(header).order(ByteOrder.LITTLE_ENDIAN)
            val channels = buffer.getShort(22).toInt().coerceAtLeast(1)
            val sampleRate = buffer.getInt(24).coerceAtLeast(1)
            val bitsPerSample = buffer.getShort(34).toInt().coerceAtLeast(8)
            val dataBytes = (wav.length() - 44).coerceAtLeast(0)
            return dataBytes.toDouble() / (sampleRate * channels * (bitsPerSample / 8.0))
        }
    }

    private fun playAndWait(file: File, durationSeconds: Double): Boolean {
        repeat(MAX_PLAYBACK_ATTEMPTS) { attempt ->
            if (playOnceAndWait(file, durationSeconds)) return true
            if (attempt + 1 < MAX_PLAYBACK_ATTEMPTS) Thread.sleep(PLAYBACK_RETRY_DELAY_MS)
        }
        return false
    }

    private fun playOnceAndWait(file: File, durationSeconds: Double): Boolean {
        val completed = AtomicBoolean(false)
        val latch = CountDownLatch(1)
        val active = AtomicReference<MediaPlayer?>()
        onMain {
            MediaPlayer().also { player ->
                active.set(player)
                player.apply {
                setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build(),
                )
                setDataSource(file.absolutePath)
                setOnCompletionListener {
                    completed.set(true)
                    active.compareAndSet(it, null)
                    it.release()
                    latch.countDown()
                }
                setOnErrorListener { player, _, _ ->
                    active.compareAndSet(player, null)
                    player.release()
                    latch.countDown()
                    true
                }
                setOnPreparedListener(MediaPlayer::start)
                prepareAsync()
                }
            }
        }
        val timeout = durationSeconds.toLong().coerceAtLeast(1L) + 15L
        val finished = latch.await(timeout, TimeUnit.SECONDS) && completed.get()
        if (!finished) {
            val timedOut = active.getAndSet(null)
            if (timedOut != null) onMain { runCatching { timedOut.release() } }
        }
        return finished
    }

    @Suppress("DEPRECATION")
    private fun recordMicrophoneProbe(): Long {
        val output = File(filesDir, OUTPUT_AUDIO_NOTE)
        output.delete()
        val recorder = MediaRecorder().apply {
            setAudioSource(MediaRecorder.AudioSource.MIC)
            setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
            setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
            setAudioSamplingRate(44_100)
            setAudioEncodingBitRate(96_000)
            setOutputFile(output.absolutePath)
            prepare()
            start()
        }
        Thread.sleep(1_500)
        try {
            recorder.stop()
        } finally {
            recorder.release()
        }
        return output.length()
    }

    private fun runStabilityProbe(requestedSeconds: Int, voice: LocalTtsVoice): JSONObject {
        val result = JSONObject()
            .put("requested_seconds", requestedSeconds)
            .put("iterations", 0)
            .put("elapsed_seconds", 0.0)
            .put("max_rtf", 0.0)
        if (requestedSeconds == 0) return result.put("ok", true)

        val started = SystemClock.elapsedRealtime()
        var iterations = 0
        var maxRtf = 0.0
        SherpaLocalTtsEngine(this).useEngine { engine ->
            while (SystemClock.elapsedRealtime() - started < requestedSeconds * 1000L) {
                val text = "$PROBE_TEXT 连续朗读第${iterations + 1}段。"
                val synthStarted = SystemClock.elapsedRealtime()
                val audio = engine.synthesize(text, voice, 100)
                val synthMs = SystemClock.elapsedRealtime() - synthStarted
                val duration = wavDurationSeconds(audio)
                require(duration > 0.1 && playAndWait(audio, duration)) { "stability playback failed at iteration ${iterations + 1}" }
                maxRtf = maxOf(maxRtf, synthMs / 1000.0 / duration)
                iterations += 1
            }
        }
        val elapsed = (SystemClock.elapsedRealtime() - started) / 1000.0
        return result
            .put("ok", elapsed >= requestedSeconds)
            .put("iterations", iterations)
            .put("elapsed_seconds", elapsed)
            .put("max_rtf", maxRtf)
    }

    private fun runForegroundControlProbe(modelId: String, voiceId: String): JSONObject {
        val preferences = ReaderPreferences(
            ttsMode = ReaderTtsMode.LOCAL_PRIVATE,
            ttsLocalModelId = modelId,
            ttsLocalVoiceId = voiceId,
            ttsTimerMinutes = 30,
        )
        val segments = List(20) { index -> "$PROBE_TEXT 后台朗读第${index + 1}段。" }
        fun startSession() {
            onMain {
                ReaderTtsSession.start(
                    this,
                    segments,
                    preferences,
                    ReaderTtsNetworkConfig("", "", "", "dynamic-probe", ""),
                )
            }
        }

        startSession()
        val initial = waitForSnapshot("initial playback") { it.state == ReaderTtsState.PLAYING }
        onMain(ReaderTtsSession::pauseOrResume)
        val paused = waitForSnapshot("pause") { it.state == ReaderTtsState.PAUSED }
        onMain(ReaderTtsSession::pauseOrResume)
        val resumed = waitForSnapshot("resume") { it.state == ReaderTtsState.PLAYING }

        val beforeNext = resumed.index
        onMain(ReaderTtsSession::next)
        val next = waitForSnapshot("next") { it.index == (beforeNext + 1).coerceAtMost(it.total - 1) }
        onMain(ReaderTtsSession::previous)
        val previous = waitForSnapshot("previous") { it.index == (next.index - 1).coerceAtLeast(0) }

        onMain { ReaderTtsSession.setRate(120) }
        val rated = waitForSnapshot("rate") { it.ratePercent == 120 }
        onMain { ReaderTtsSession.setTimer(30) }
        val timed = waitForSnapshot("timer") { it.timerMinutes == 30 }
        onMain(ReaderTtsSession::stop)
        val stopped = waitForSnapshot("stop") { it.state == ReaderTtsState.STOPPED }

        startSession()
        val restarted = waitForSnapshot("restart") {
            it.state == ReaderTtsState.PREPARING || it.state == ReaderTtsState.PLAYING
        }
        onMain(ReaderTtsSession::stop)
        waitForSnapshot("restart stop") { it.state == ReaderTtsState.STOPPED }

        val continuationRequested = AtomicBoolean(false)
        onMain {
            ReaderTtsSession.setContinuationProvider(
                ReaderTtsContinuationProvider { direction, onReady ->
                    if (direction > 0 && continuationRequested.compareAndSet(false, true)) {
                        onReady(listOf(CONTINUATION_TEXT))
                    } else {
                        onReady(emptyList())
                    }
                },
            )
            ReaderTtsSession.start(
                this,
                listOf(CONTINUATION_SOURCE_TEXT),
                preferences,
                ReaderTtsNetworkConfig("", "", "", "dynamic-probe", "chapter-1"),
                continueAcrossPages = true,
            )
        }
        val continued = waitForSnapshot("chapter continuation", timeoutSeconds = 45) {
            it.currentText.contains(CONTINUATION_MARKER) &&
                (it.state == ReaderTtsState.PREPARING || it.state == ReaderTtsState.PLAYING)
        }
        onMain(ReaderTtsSession::stop)
        waitForSnapshot("continuation stop") { it.state == ReaderTtsState.STOPPED }

        onMain {
            ReaderTtsSession.setContinuationProvider(null)
            ReaderTtsSession.start(
                this,
                listOf(TIMER_TEXT),
                preferences,
                ReaderTtsNetworkConfig("", "", "", "dynamic-probe", "timer"),
            )
        }
        waitForSnapshot("timer playback") {
            it.state == ReaderTtsState.PREPARING || it.state == ReaderTtsState.PLAYING
        }
        onMain { ReaderTtsSession.setTimerForDebug(DEBUG_TIMER_MS) }
        val timerStopped = waitForSnapshot("debug timer stop", timeoutSeconds = 10) {
            it.state == ReaderTtsState.STOPPED
        }
        return JSONObject()
            .put("started", true)
            .put("controls_verified", true)
            .put("initial_state", initial.state.name.lowercase())
            .put("pause_state", paused.state.name.lowercase())
            .put("resume_state", resumed.state.name.lowercase())
            .put("next_index", next.index)
            .put("previous_index", previous.index)
            .put("rate_percent", rated.ratePercent)
            .put("timer_minutes", timed.timerMinutes)
            .put("stop_state", stopped.state.name.lowercase())
            .put("restart_state", restarted.state.name.lowercase())
            .put("continuation_requested", continuationRequested.get())
            .put("continuation_text", continued.currentText)
            .put("continuation_state", continued.state.name.lowercase())
            .put("debug_timer_ms", DEBUG_TIMER_MS)
            .put("debug_timer_stop_state", timerStopped.state.name.lowercase())
    }

    private fun onMain(action: () -> Unit) {
        val failure = arrayOfNulls<Throwable>(1)
        val latch = CountDownLatch(1)
        runOnUiThread {
            runCatching(action).onFailure { failure[0] = it }
            latch.countDown()
        }
        require(latch.await(15, TimeUnit.SECONDS)) { "main-thread TTS control timed out" }
        failure[0]?.let { throw it }
    }

    private fun waitForSnapshot(
        label: String,
        timeoutSeconds: Long = 30,
        predicate: (ReaderTtsSnapshot) -> Boolean,
    ): ReaderTtsSnapshot {
        val deadline = SystemClock.elapsedRealtime() + timeoutSeconds * 1000L
        var latest = ReaderTtsSession.current()
        while (SystemClock.elapsedRealtime() < deadline) {
            latest = ReaderTtsSession.current()
            if (predicate(latest)) return latest
            Thread.sleep(50)
        }
        error("$label did not reach the expected state; latest=$latest")
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private inline fun <T> SherpaLocalTtsEngine.useEngine(block: (SherpaLocalTtsEngine) -> T): T =
        try {
            block(this)
        } finally {
            release()
        }

    companion object {
        const val EXTRA_ARCHIVE_PATH = "archive_path"
        const val EXTRA_STABILITY_SECONDS = "stability_seconds"
        const val EXTRA_FOREGROUND_SERVICE = "foreground_service"
        const val RESULT_FILE = "click-tts-probe-result.json"
        const val OUTPUT_WAV = "click-tts-probe.wav"
        const val OUTPUT_AUDIO_NOTE = "click-audio-note-probe.m4a"
        private const val CONTINUATION_MARKER = "第二章动态续读"
        private const val CONTINUATION_SOURCE_TEXT = "第一章结束。"
        private const val CONTINUATION_TEXT = "$CONTINUATION_MARKER 已经开始。"
        private const val TIMER_TEXT =
            "这是一段专门用于倒计时动态验收的较长朗读文字，计时结束以后播放会话必须停止，并且释放前台播放状态。"
        private const val DEBUG_TIMER_MS = 1_500L
        private const val DEFAULT_ARCHIVE = "click-local-tts-probe.zip"
        private const val DEFAULT_VOICE_ID = "kokoro-zm-009"
        private const val LOG_TAG = "ClickLocalTtsProbe"
        private const val PROBE_TEXT = "生命读经帮助我们在日常生活中经历基督。Grace supplies us to live and serve."
        private const val MAX_PLAYBACK_ATTEMPTS = 2
        private const val PLAYBACK_RETRY_DELAY_MS = 250L
    }
}
