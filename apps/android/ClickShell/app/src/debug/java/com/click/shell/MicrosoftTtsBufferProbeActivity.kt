package com.click.shell

import android.app.Activity
import android.os.Bundle
import android.os.SystemClock
import android.widget.TextView
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.File
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.charset.StandardCharsets
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import kotlin.concurrent.thread

/** Debug-only localhost probe for the persistent 60/90 minute Microsoft audio buffer. */
class MicrosoftTtsBufferProbeActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val status = TextView(this).apply {
            text = "Click Microsoft buffer probe is running"
            textSize = 18f
            setPadding(24, 24, 24, 24)
        }
        setContentView(status)
        thread(name = "click-microsoft-buffer-probe") {
            val result = runProbe()
            File(filesDir, RESULT_FILE).writeText(result.toString(2), Charsets.UTF_8)
            runOnUiThread {
                status.text = if (result.optBoolean("ok")) "PASS" else "FAIL\n${result.optString("error")}"
            }
        }
    }

    private fun runProbe(): JSONObject {
        val result = JSONObject()
            .put("schema", "click.android.microsoft_tts_buffer.dynamic_probe.v1")
            .put("low_water_minutes", ReaderTtsBufferPolicy.LOW_WATER_MINUTES)
            .put("high_water_minutes", ReaderTtsBufferPolicy.HIGH_WATER_MINUTES)
        return runCatching {
            val audio = File(intent.getStringExtra(EXTRA_AUDIO_PATH).orEmpty())
            require(audio.isFile && audio.length() > 512L) { "probe audio is missing" }
            val server = FakeTtsServer(audio)
            try {
                val provider = ReaderTtsLookaheadProvider { _, _, _ ->
                    ReaderTtsLookaheadPlan(
                        listOf(
                            ReaderTtsLookaheadSegment("缓存动态验收第一段。", "chapter-1"),
                            ReaderTtsLookaheadSegment("缓存动态验收第二段。", "chapter-1"),
                            ReaderTtsLookaheadSegment("缓存动态验收第三段。", "chapter-2"),
                        ),
                        reachedBookEnd = true,
                    )
                }
                onMain {
                    ReaderTtsSession.setLookaheadProvider(provider)
                    ReaderTtsSession.setContinuationProvider(
                        ReaderTtsContinuationProvider { _, onReady -> onReady(emptyList()) },
                    )
                    ReaderTtsSession.start(
                        this,
                        listOf("缓存动态验收第一段。"),
                        ReaderPreferences(
                            ttsMode = ReaderTtsMode.MICROSOFT_ONLINE,
                            ttsTimerMinutes = 30,
                        ),
                        ReaderTtsNetworkConfig(
                            baseUrl = "http://127.0.0.1:${server.port}",
                            deviceId = "buffer-probe",
                            accessToken = "",
                            bookId = "buffer-probe-book",
                            locator = "chapter-1",
                        ),
                        continueAcrossPages = true,
                    )
                }
                val playing = waitForSnapshot("Microsoft playback") {
                    it.state == ReaderTtsState.PLAYING
                }
                val filled = waitForSnapshot("60 minute buffer", timeoutSeconds = 45) {
                    it.bufferedMinutes >= ReaderTtsBufferPolicy.LOW_WATER_MINUTES &&
                        !it.bufferFilling
                }
                onMain(ReaderTtsSession::stop)
                val stopped = waitForSnapshot("buffer probe stop") {
                    it.state == ReaderTtsState.STOPPED
                }
                val cacheFiles = File(filesDir, "click-reader-tts-audio/v1")
                    .listFiles()
                    .orEmpty()
                    .filter { it.isFile && it.extension == "audio" }
                require(cacheFiles.isNotEmpty()) { "no durable Microsoft audio cache was committed" }
                require(filled.bufferedMinutes >= ReaderTtsBufferPolicy.HIGH_WATER_MINUTES) {
                    "refill stopped before the 90 minute high-water mark"
                }
                require(server.postRequests.get() == server.audioRequests.get()) {
                    "each synthesis request must have exactly one audio download"
                }
                require(server.audioRequests.get() == cacheFiles.size) {
                    "high-water refill continued after its committed cache entries"
                }
                server.close()
                onMain {
                    ReaderTtsSession.start(
                        this,
                        listOf("缓存动态验收第一段。"),
                        ReaderPreferences(
                            ttsMode = ReaderTtsMode.MICROSOFT_ONLINE,
                            ttsTimerMinutes = 30,
                        ),
                        ReaderTtsNetworkConfig(
                            baseUrl = "http://127.0.0.1:${server.port}",
                            deviceId = "buffer-probe",
                            accessToken = "",
                            bookId = "buffer-probe-book",
                            locator = "chapter-1",
                        ),
                        continueAcrossPages = true,
                    )
                }
                val offlinePlaying = waitForSnapshot("offline cached Microsoft playback") {
                    it.state == ReaderTtsState.PLAYING
                }
                val offlineBuffered = waitForSnapshot("offline cached horizon") {
                    it.bufferedMinutes >= ReaderTtsBufferPolicy.LOW_WATER_MINUTES &&
                        !it.bufferFilling
                }
                onMain(ReaderTtsSession::stop)
                waitForSnapshot("offline cached stop") {
                    it.state == ReaderTtsState.STOPPED
                }
                result
                    .put("ok", true)
                    .put("playing_state", playing.state.name.lowercase())
                    .put("buffered_minutes", filled.bufferedMinutes)
                    .put("buffer_filling", filled.bufferFilling)
                    .put("stopped_state", stopped.state.name.lowercase())
                    .put("tts_post_requests", server.postRequests.get())
                    .put("audio_get_requests", server.audioRequests.get())
                    .put("cache_file_count", cacheFiles.size)
                    .put("cache_bytes", cacheFiles.sumOf(File::length))
                    .put("cache_persistent_after_stop", cacheFiles.all(File::isFile))
                    .put("offline_cache_playing_state", offlinePlaying.state.name.lowercase())
                    .put("offline_cache_buffered_minutes", offlineBuffered.bufferedMinutes)
                    .put("offline_network_requests", server.postRequests.get() - cacheFiles.size)
            } finally {
                onMain {
                    ReaderTtsSession.stop()
                    ReaderTtsSession.setLookaheadProvider(null)
                    ReaderTtsSession.setContinuationProvider(null)
                }
                server.close()
            }
        }.getOrElse { error ->
            result
                .put("ok", false)
                .put("error", "${error.javaClass.simpleName}: ${error.message}")
        }
    }

    private fun onMain(action: () -> Unit) {
        val completed = AtomicBoolean(false)
        val failure = arrayOfNulls<Throwable>(1)
        runOnUiThread {
            runCatching(action).onFailure { failure[0] = it }
            completed.set(true)
        }
        val deadline = SystemClock.elapsedRealtime() + 15_000L
        while (!completed.get() && SystemClock.elapsedRealtime() < deadline) Thread.sleep(10L)
        require(completed.get()) { "main-thread action timed out" }
        failure[0]?.let { throw it }
    }

    private fun waitForSnapshot(
        label: String,
        timeoutSeconds: Long = 30,
        predicate: (ReaderTtsSnapshot) -> Boolean,
    ): ReaderTtsSnapshot {
        val deadline = SystemClock.elapsedRealtime() + timeoutSeconds * 1_000L
        var latest = ReaderTtsSession.current()
        while (SystemClock.elapsedRealtime() < deadline) {
            latest = ReaderTtsSession.current()
            if (predicate(latest)) return latest
            Thread.sleep(50L)
        }
        error("$label did not reach the expected state; latest=$latest")
    }

    private class FakeTtsServer(private val audio: File) : AutoCloseable {
        private val running = AtomicBoolean(true)
        private val socket = ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"))
        val postRequests = AtomicInteger(0)
        val audioRequests = AtomicInteger(0)
        val port: Int = socket.localPort
        private val worker = thread(name = "click-fake-microsoft-tts") {
            while (running.get()) {
                val client = runCatching { socket.accept() }.getOrNull() ?: break
                runCatching { handle(client) }
            }
        }

        private fun handle(client: Socket) {
            client.use { connection ->
                val input = BufferedInputStream(connection.getInputStream())
                val requestLine = readLine(input)
                var contentLength = 0
                while (true) {
                    val line = readLine(input)
                    if (line.isEmpty()) break
                    if (line.startsWith("Content-Length:", ignoreCase = true)) {
                        contentLength = line.substringAfter(':').trim().toIntOrNull() ?: 0
                    }
                }
                repeat(contentLength) { input.read() }
                val output = BufferedOutputStream(connection.getOutputStream())
                if (requestLine.startsWith("POST /v1/android/tts ")) {
                    postRequests.incrementAndGet()
                    val body = JSONObject()
                        .put("ok", true)
                        .put("audio_url", "/probe-audio")
                        .toString()
                        .toByteArray(StandardCharsets.UTF_8)
                    writeHeaders(output, 200, "application/json", body.size.toLong())
                    output.write(body)
                } else if (requestLine.startsWith("GET /probe-audio ")) {
                    audioRequests.incrementAndGet()
                    writeHeaders(output, 200, "audio/mpeg", audio.length())
                    audio.inputStream().use { it.copyTo(output) }
                } else {
                    val body = "not found".toByteArray(StandardCharsets.UTF_8)
                    writeHeaders(output, 404, "text/plain", body.size.toLong())
                    output.write(body)
                }
                output.flush()
            }
        }

        private fun readLine(input: BufferedInputStream): String {
            val bytes = mutableListOf<Byte>()
            while (true) {
                val value = input.read()
                if (value < 0 || value == '\n'.code) break
                if (value != '\r'.code) bytes += value.toByte()
            }
            return bytes.toByteArray().toString(StandardCharsets.US_ASCII)
        }

        private fun writeHeaders(
            output: BufferedOutputStream,
            status: Int,
            contentType: String,
            contentLength: Long,
        ) {
            val reason = if (status == 200) "OK" else "Not Found"
            output.write(
                (
                    "HTTP/1.1 $status $reason\r\n" +
                        "Content-Type: $contentType\r\n" +
                        "Content-Length: $contentLength\r\n" +
                        "Connection: close\r\n\r\n"
                    ).toByteArray(StandardCharsets.US_ASCII),
            )
        }

        override fun close() {
            running.set(false)
            runCatching { socket.close() }
            worker.join(2_000L)
        }
    }

    companion object {
        const val EXTRA_AUDIO_PATH = "audio_path"
        const val RESULT_FILE = "click-microsoft-buffer-probe-result.json"
    }
}
