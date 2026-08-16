package com.click.shell

import android.content.Context
import android.media.MediaMetadataRetriever
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.security.MessageDigest

data class ReaderTtsCachedAudio(
    val key: String,
    val file: File,
    val durationMs: Long,
)

/** Durable, bounded cache for already playable compressed Microsoft speech audio. */
class ReaderTtsAudioCache(context: Context) {
    private val root = File(context.applicationContext.filesDir, "click-reader-tts-audio/v1").apply {
        mkdirs()
    }

    fun stableKey(
        bookId: String,
        engine: String,
        voice: String,
        ratePercent: Int,
        text: String,
    ): String {
        val normalized = ReaderTtsTextNormalizer.normalize(text)
        return sha256("$bookId|$engine|$voice|${ratePercent.coerceIn(80, 150)}|$normalized")
    }

    @Synchronized
    fun find(key: String): ReaderTtsCachedAudio? {
        if (!SAFE_KEY.matches(key)) return null
        val audio = audioFile(key)
        if (!audio.isFile || audio.length() <= MIN_AUDIO_BYTES) {
            deleteEntry(key)
            return null
        }
        val metadata = metadataFile(key)
        val duration = runCatching {
            if (metadata.isFile) {
                JSONObject(metadata.readText(Charsets.UTF_8)).optLong("duration_ms", 0L)
            } else {
                0L
            }
        }.getOrDefault(0L).takeIf { it > 0L } ?: durationMs(audio)
        if (duration <= 0L) {
            deleteEntry(key)
            return null
        }
        if (!metadata.isFile) writeMetadata(key, duration, "", "", "", 100)
        val now = System.currentTimeMillis()
        audio.setLastModified(now)
        metadata.setLastModified(now)
        return ReaderTtsCachedAudio(key, audio, duration)
    }

    fun newPartial(key: String): File {
        require(SAFE_KEY.matches(key)) { "invalid TTS cache key" }
        root.mkdirs()
        return File(root, "$key-${System.nanoTime()}.partial")
    }

    @Synchronized
    fun commit(
        key: String,
        partial: File,
        bookId: String,
        chapterLocator: String,
        voice: String,
        ratePercent: Int,
    ): ReaderTtsCachedAudio {
        require(SAFE_KEY.matches(key)) { "invalid TTS cache key" }
        require(partial.parentFile?.canonicalFile == root.canonicalFile) { "TTS cache staging path escaped" }
        find(key)?.let {
            partial.delete()
            return it
        }
        require(partial.isFile && partial.length() > MIN_AUDIO_BYTES) { "微软语音缓存为空" }
        val duration = durationMs(partial)
        require(duration > 0L) { "微软语音缓存无法读取时长" }
        val target = audioFile(key)
        if (target.exists()) target.delete()
        check(partial.renameTo(target)) { "微软语音缓存原子提交失败" }
        writeMetadata(key, duration, bookId, chapterLocator, voice, ratePercent)
        trim(setOf(key))
        return ReaderTtsCachedAudio(key, target, duration)
    }

    @Synchronized
    fun deleteEntry(key: String) {
        if (!SAFE_KEY.matches(key)) return
        audioFile(key).delete()
        metadataFile(key).delete()
    }

    @Synchronized
    fun trim(protectedKeys: Set<String> = emptySet()) {
        val files = root.listFiles()
            ?.filter { it.isFile && it.extension == AUDIO_EXTENSION }
            ?.sortedBy(File::lastModified)
            .orEmpty()
        var total = files.sumOf(File::length)
        for (file in files) {
            if (total <= MAX_CACHE_BYTES) break
            val key = file.nameWithoutExtension
            if (key in protectedKeys) continue
            val length = file.length()
            if (file.delete()) {
                total -= length
                metadataFile(key).delete()
            }
        }
        root.listFiles()
            ?.filter { it.isFile && it.extension == "partial" && System.currentTimeMillis() - it.lastModified() > PARTIAL_MAX_AGE_MS }
            ?.forEach(File::delete)
    }

    private fun writeMetadata(
        key: String,
        durationMs: Long,
        bookId: String,
        chapterLocator: String,
        voice: String,
        ratePercent: Int,
    ) {
        val target = metadataFile(key)
        val pending = File(root, "$key-${System.nanoTime()}.meta.partial")
        val payload = JSONObject()
            .put("schema", "click.reader_tts_audio_cache.v1")
            .put("key", key)
            .put("duration_ms", durationMs)
            .put("book_id", bookId)
            .put("chapter_locator", chapterLocator)
            .put("voice", voice)
            .put("rate_percent", ratePercent.coerceIn(80, 150))
            .put("updated_at_ms", System.currentTimeMillis())
        FileOutputStream(pending).use { output ->
            output.write(payload.toString().toByteArray(Charsets.UTF_8))
            output.fd.sync()
        }
        if (target.exists()) target.delete()
        check(pending.renameTo(target)) { "TTS cache metadata commit failed" }
    }

    private fun durationMs(file: File): Long {
        val retriever = MediaMetadataRetriever()
        return try {
            retriever.setDataSource(file.absolutePath)
            retriever.extractMetadata(MediaMetadataRetriever.METADATA_KEY_DURATION)
                ?.toLongOrNull()
                ?.coerceAtLeast(0L)
                ?: 0L
        } catch (_: Throwable) {
            0L
        } finally {
            runCatching { retriever.release() }
        }
    }

    private fun audioFile(key: String): File = File(root, "$key.$AUDIO_EXTENSION")

    private fun metadataFile(key: String): File = File(root, "$key.json")

    private fun sha256(value: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    companion object {
        private val SAFE_KEY = Regex("[a-f0-9]{64}")
        private const val AUDIO_EXTENSION = "audio"
        private const val MIN_AUDIO_BYTES = 512L
        private const val MAX_CACHE_BYTES = 512L * 1024L * 1024L
        private const val PARTIAL_MAX_AGE_MS = 24L * 60L * 60L * 1_000L
    }
}
