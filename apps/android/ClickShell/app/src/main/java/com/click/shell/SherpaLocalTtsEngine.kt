package com.click.shell

import android.content.Context
import com.k2fsa.sherpa.onnx.OfflineTts
import com.k2fsa.sherpa.onnx.getOfflineTtsConfig
import java.io.File
import java.security.MessageDigest

class SherpaLocalTtsEngine(context: Context) {
    private val cacheRoot = File(context.applicationContext.cacheDir, "click-local-tts-audio").apply { mkdirs() }
    private var loadedVoiceKey = ""
    private var tts: OfflineTts? = null

    @Synchronized
    fun synthesize(text: String, voice: LocalTtsVoice, ratePercent: Int): File {
        val normalized = ReaderTtsTextNormalizer.normalize(text)
        require(normalized.isNotBlank()) { "没有可朗读文字" }
        val rate = ratePercent.coerceIn(80, 150)
        val output = File(cacheRoot, "${sha256("${voice.voiceId}|$rate|$normalized")}.wav")
        if (output.isFile && output.length() > MIN_AUDIO_BYTES) {
            output.setLastModified(System.currentTimeMillis())
            return output
        }

        val engine = load(voice)
        val generated = engine.generate(
            normalized,
            sid = voice.speakerId,
            speed = voice.defaultSpeed * rate / 100f,
        )
        val pending = File(output.parentFile, "${output.name}.partial")
        pending.delete()
        check(generated.save(pending.absolutePath)) { "本地 TTS 未能写入音频" }
        check(pending.isFile && pending.length() > MIN_AUDIO_BYTES) { "本地 TTS 生成了无效音频" }
        if (output.exists()) output.delete()
        check(pending.renameTo(output)) { "本地 TTS 音频缓存落盘失败" }
        trimCache(output)
        return output
    }

    @Synchronized
    fun release() {
        tts?.release()
        tts = null
        loadedVoiceKey = ""
    }

    @Synchronized
    private fun load(voice: LocalTtsVoice): OfflineTts {
        val key = "${voice.modelId}:${voice.modelRoot.canonicalPath}"
        if (loadedVoiceKey == key && tts != null) return requireNotNull(tts)
        release()
        val config = getOfflineTtsConfig(
            modelDir = voice.modelRoot.absolutePath,
            modelName = voice.modelFile,
            acousticModelName = "",
            vocoder = "",
            voices = voice.voicesFile,
            // sherpa's helper prefixes a single lexicon with modelDir. Passing an already
            // absolute single path would create "modelDir/absolutePath" and fail on device.
            lexicon = singleModelRelativePath(voice.modelRoot, voice.lexiconFile),
            dataDir = voice.dataDirectory,
            dictDir = "",
            ruleFsts = voice.ruleFsts,
            ruleFars = voice.ruleFars,
            numThreads = 4,
        )
        return OfflineTts(assetManager = null, config = config).also {
            tts = it
            loadedVoiceKey = key
        }
    }

    private fun singleModelRelativePath(root: File, value: String): String {
        if (value.isBlank() || ',' in value) return value
        val file = File(value)
        if (!file.isAbsolute) return value
        return runCatching { file.canonicalFile.relativeTo(root.canonicalFile).path }
            .getOrDefault(value)
    }

    private fun sha256(value: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    private fun trimCache(keep: File) {
        val files = cacheRoot.listFiles()
            ?.filter { it.isFile && it.extension == "wav" }
            ?.sortedBy(File::lastModified)
            .orEmpty()
        var total = files.sumOf(File::length)
        files.forEach { file ->
            if (total <= MAX_CACHE_BYTES) return
            if (file.canonicalPath == keep.canonicalPath) return@forEach
            val length = file.length()
            if (file.delete()) total -= length
        }
    }

    companion object {
        private const val MIN_AUDIO_BYTES = 512L
        private const val MAX_CACHE_BYTES = 128L * 1024L * 1024L
    }
}

object ReaderTtsTextNormalizer {
    private val whitespace = Regex("\\s+")
    private val partOfSpeech = mapOf(
        "n." to "名词",
        "v." to "动词",
        "vt." to "及物动词",
        "vi." to "不及物动词",
        "adj." to "形容词",
        "adv." to "副词",
        "prep." to "介词",
        "pron." to "代词",
        "conj." to "连词",
    )

    fun normalize(value: String): String {
        var result = value
            .replace('\u00a0', ' ')
            .replace('—', '，')
            .replace('–', '，')
            .replace(whitespace, " ")
            .trim()
        partOfSpeech.forEach { (short, spoken) ->
            result = result.replace(Regex("(?i)(?<![A-Za-z])${Regex.escape(short)}(?![A-Za-z])"), spoken)
        }
        return result
    }

    fun segment(value: String): List<String> {
        return value
            .split(Regex("[\\r\\n]+"))
            .flatMap(::segmentLine)
    }

    private fun segmentLine(value: String): List<String> {
        val normalized = normalize(value)
        if (normalized.isBlank()) return emptyList()
        val result = mutableListOf<String>()
        var start = 0
        normalized.forEachIndexed { index, char ->
            if (char in SENTENCE_ENDINGS) {
                normalized.substring(start, index + 1).trim().takeIf(String::isNotBlank)?.let(result::add)
                start = index + 1
            }
        }
        normalized.substring(start).trim().takeIf(String::isNotBlank)?.let(result::add)
        return result.flatMap(::splitLongSentence)
    }

    private fun splitLongSentence(value: String): List<String> {
        if (value.length <= MAX_SEGMENT_LENGTH) return listOf(value)
        val pieces = value.split(Regex("(?<=[，；：,;:])"))
        val result = mutableListOf<String>()
        var buffer = ""
        pieces.forEach { piece ->
            if (buffer.isNotBlank() && buffer.length + piece.length > MAX_SEGMENT_LENGTH) {
                result += buffer.trim()
                buffer = ""
            }
            buffer += piece
        }
        buffer.trim().takeIf(String::isNotBlank)?.let(result::add)
        return result
    }

    private val SENTENCE_ENDINGS = setOf('。', '！', '？', '；', '.', '!', '?', ';')
    private const val MAX_SEGMENT_LENGTH = 160
}
