package com.click.shell

import android.content.Context
import android.net.Uri
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.security.MessageDigest
import java.util.zip.ZipInputStream

data class LocalTtsVoice(
    val modelId: String,
    val voiceId: String,
    val displayName: String,
    val modelRoot: File,
    val modelFile: String,
    val voicesFile: String,
    val tokensFile: String,
    val lexiconFile: String,
    val dataDirectory: String,
    val ruleFsts: String,
    val ruleFars: String,
    val speakerId: Int,
    val defaultSpeed: Float,
)

data class LocalTtsImportResult(
    val modelId: String,
    val archiveSha256: String,
    val fileCount: Int,
    val voices: List<LocalTtsVoice>,
)

/** Installs user-selected offline voice packages without putting book text or models on the network. */
class LocalTtsModelManager(context: Context) {
    private val appContext = context.applicationContext
    private val modelRoot = File(appContext.filesDir, "click-local-tts-models").apply { mkdirs() }
    private val importRoot = File(appContext.cacheDir, "click-local-tts-import").apply { mkdirs() }

    fun modelDirectory(): File = modelRoot

    fun installedVoices(): List<LocalTtsVoice> =
        modelRoot.listFiles()
            ?.filter { it.isDirectory }
            .orEmpty()
            .flatMap(::readVoices)

    fun selectedVoice(modelId: String, voiceId: String): LocalTtsVoice? {
        val voices = installedVoices()
        return voices.firstOrNull { it.modelId == modelId && it.voiceId == voiceId }
            ?: voices.firstOrNull { it.modelId == modelId }
            ?: voices.firstOrNull()
    }

    fun importSupportedPackage(uri: Uri): LocalTtsImportResult {
        importRoot.mkdirs()
        val archive = File(importRoot, "voice-${System.nanoTime()}.zip")
        val archiveSha = copyAndHash(uri, archive)
        require(archiveSha == KOKORO_ARCHIVE_SHA256) {
            "语音包 SHA256 不匹配，已拒绝安装。实际值：$archiveSha"
        }

        val staging = File(importRoot, "staging-${System.nanoTime()}")
        staging.mkdirs()
        val fileCount = try {
            extractSafely(archive, staging)
        } finally {
            archive.delete()
        }
        val config = staging.walkTopDown().firstOrNull { it.isFile && it.name == "voice.json" }
            ?: error("语音包缺少 voice.json")
        val unpackedRoot = requireNotNull(config.parentFile)
        val json = JSONObject(config.readText(Charsets.UTF_8))
        val modelId = json.optString("id").ifBlank { ReaderPreferences.DEFAULT_LOCAL_MODEL }
        require(modelId.matches(Regex("[A-Za-z0-9._-]+"))) { "语音包 model id 不合法" }
        validateRequiredFiles(unpackedRoot, json)

        val target = File(modelRoot, modelId)
        val backup = File(importRoot, "backup-${System.nanoTime()}")
        if (target.exists() && !target.renameTo(backup)) error("无法替换已有语音包")
        try {
            if (!unpackedRoot.renameTo(target)) {
                unpackedRoot.copyRecursively(target, overwrite = true)
            }
            writeInstallManifest(target, modelId, archiveSha, fileCount)
            verifiedModelPaths.remove(target.canonicalPath)
            val voices = readVoices(target)
            require(voices.isNotEmpty()) { "语音包没有可用声线" }
            backup.deleteRecursively()
            staging.deleteRecursively()
            return LocalTtsImportResult(modelId, archiveSha, fileCount, voices)
        } catch (error: Throwable) {
            target.deleteRecursively()
            if (backup.exists()) backup.renameTo(target)
            staging.deleteRecursively()
            throw error
        }
    }

    fun removeModel(modelId: String): Boolean {
        if (!modelId.matches(Regex("[A-Za-z0-9._-]+"))) return false
        val target = File(modelRoot, modelId)
        if (!target.exists() || target.canonicalFile.parentFile != modelRoot.canonicalFile) return false
        verifiedModelPaths.remove(target.canonicalPath)
        return target.deleteRecursively()
    }

    fun modelSizeBytes(modelId: String): Long {
        if (!modelId.matches(Regex("[A-Za-z0-9._-]+"))) return 0L
        val target = File(modelRoot, modelId)
        if (!target.isDirectory || target.canonicalFile.parentFile != modelRoot.canonicalFile) return 0L
        return target.walkTopDown().filter(File::isFile).sumOf(File::length)
    }

    private fun copyAndHash(uri: Uri, target: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        appContext.contentResolver.openInputStream(uri).use { input ->
            requireNotNull(input) { "无法打开语音包" }
            FileOutputStream(target).use { output ->
                val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                var total = 0L
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    total += count
                    require(total <= MAX_ARCHIVE_BYTES) { "语音包超过 ${MAX_ARCHIVE_BYTES / 1024 / 1024} MB 限制" }
                    digest.update(buffer, 0, count)
                    output.write(buffer, 0, count)
                }
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun extractSafely(archive: File, destination: File): Int {
        var fileCount = 0
        var expandedBytes = 0L
        val destinationPath = destination.canonicalPath + File.separator
        ZipInputStream(FileInputStream(archive).buffered()).use { zip ->
            while (true) {
                val entry = zip.nextEntry ?: break
                val isDirectory = entry.isDirectory || entry.name.endsWith('\\')
                val normalized = entry.name.replace('\\', '/').trimStart('/')
                val parts = normalized.split('/').filter { it.isNotBlank() && it != "." }
                require(parts.isNotEmpty() && parts.none { it == ".." }) { "语音包包含不安全路径" }
                val target = File(destination, parts.joinToString(File.separator)).canonicalFile
                require(target.path.startsWith(destinationPath)) { "语音包路径越界" }
                if (isDirectory) {
                    target.mkdirs()
                } else {
                    target.parentFile?.mkdirs()
                    FileOutputStream(target).use { output ->
                        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                        while (true) {
                            val count = zip.read(buffer)
                            if (count < 0) break
                            expandedBytes += count
                            require(expandedBytes <= MAX_EXPANDED_BYTES) { "语音包解压后体积异常" }
                            output.write(buffer, 0, count)
                        }
                    }
                    fileCount += 1
                    require(fileCount <= MAX_FILE_COUNT) { "语音包文件数量异常" }
                }
                zip.closeEntry()
            }
        }
        require(fileCount > 0) { "语音包为空" }
        return fileCount
    }

    private fun validateRequiredFiles(folder: File, json: JSONObject) {
        requiredFileNames(folder, json).forEach { name ->
            require(safePackageFile(folder, name)?.isFile == true) { "语音包缺少 $name" }
        }
        val dataDirectory = json.optString("dataDir")
        if (dataDirectory.isNotBlank()) {
            require(safePackageFile(folder, dataDirectory)?.isDirectory == true) { "语音包缺少 $dataDirectory" }
        }
        require(json.optString("engine", "kokoro").equals("kokoro", ignoreCase = true)) {
            "P1.3 当前只接受已验证的 Kokoro 语音包"
        }
    }

    private fun writeInstallManifest(folder: File, modelId: String, archiveSha: String, fileCount: Int) {
        val voiceJson = JSONObject(File(folder, "voice.json").readText(Charsets.UTF_8))
        val requiredFiles = (requiredFileNames(folder, voiceJson) + "voice.json").distinct()
        val fileHashes = JSONObject()
        requiredFiles.forEach { name ->
            val file = requireNotNull(safePackageFile(folder, name)) { "语音包路径不安全：$name" }
            fileHashes.put(name, sha256(file))
        }
        val manifest = JSONObject()
            .put("schema", MODEL_MANIFEST_SCHEMA)
            .put("model_id", modelId)
            .put("runtime", "sherpa-onnx")
            .put("runtime_license", "Apache-2.0")
            .put("model_license", "Apache-2.0")
            .put("espeak_data_license", "GPL-3.0-or-later")
            .put("archive_sha256", archiveSha)
            .put("file_count", fileCount)
            .put("file_sha256", fileHashes)
            .put("local_only", true)
        File(folder, "click_install_manifest.json").writeText(manifest.toString(2), Charsets.UTF_8)
    }

    private fun requiredFileNames(folder: File, json: JSONObject): List<String> {
        val names = mutableListOf(
            json.optString("model", "model.int8.onnx"),
            json.optString("voices", "voices.bin"),
            json.optString("tokens", "tokens.txt"),
        )
        names += MODEL_NOTICE_FILES
        listOf("lexicon", "ruleFsts", "ruleFars").forEach { key ->
            names += json.optString(key).split(',').map(String::trim).filter(String::isNotBlank)
        }
        val dataDirectoryName = json.optString("dataDir")
        val dataDirectory = safePackageFile(folder, dataDirectoryName)
        if (dataDirectoryName.isNotBlank() && dataDirectory?.isDirectory == true) {
            val canonicalFolder = folder.canonicalFile
            dataDirectory.walkTopDown().filter(File::isFile).forEach { file ->
                names += file.canonicalFile.relativeTo(canonicalFolder).path.replace(File.separatorChar, '/')
            }
        }
        return names.filter(String::isNotBlank).distinct()
    }

    private fun safePackageFile(folder: File, name: String): File? {
        if (name.isBlank()) return null
        val candidate = File(name)
        if (candidate.isAbsolute) return null
        val target = File(folder, name).canonicalFile
        val root = folder.canonicalPath + File.separator
        return target.takeIf { it.path.startsWith(root) }
    }

    private fun readVoices(folder: File): List<LocalTtsVoice> {
        val config = File(folder, "voice.json")
        val installManifest = File(folder, "click_install_manifest.json")
        if (!config.isFile || !installManifest.isFile) return emptyList()
        return runCatching {
            val installJson = JSONObject(installManifest.readText(Charsets.UTF_8))
            if (installJson.optString("archive_sha256") != KOKORO_ARCHIVE_SHA256) return@runCatching emptyList()
            if (!verifyInstalledFiles(folder, installJson)) return@runCatching emptyList()
            val json = JSONObject(config.readText(Charsets.UTF_8))
            validateRequiredFiles(folder, json)
            val modelId = json.optString("id", folder.name)
            val speakers = json.optJSONArray("speakers") ?: JSONArray()
            if (speakers.length() == 0) {
                listOf(buildVoice(folder, json, modelId, modelId, json.optString("name", modelId), json.optInt("speakerId", 0)))
            } else {
                (0 until speakers.length()).mapNotNull { index ->
                    speakers.optJSONObject(index)?.let { speaker ->
                        val speakerId = speaker.optInt("speakerId", speaker.optInt("sid", 0))
                        buildVoice(
                            folder,
                            json,
                            modelId,
                            speaker.optString("id", "$modelId-$speakerId"),
                            speaker.optString("name", "声线 $speakerId"),
                            speakerId,
                        )
                    }
                }
            }
        }.getOrDefault(emptyList())
    }

    private fun verifyInstalledFiles(folder: File, manifest: JSONObject): Boolean {
        val path = folder.canonicalPath
        if (verifiedModelPaths.contains(path)) return true
        val hashes = manifest.optJSONObject("file_sha256") ?: return false
        val names = hashes.keys().asSequence().toList()
        if (names.isEmpty()) return false
        val valid = names.all { name ->
            val file = safePackageFile(folder, name) ?: return@all false
            file.isFile && runCatching { sha256(file) == hashes.optString(name) }.getOrDefault(false)
        }
        if (valid) verifiedModelPaths += path
        return valid
    }

    private fun sha256(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        FileInputStream(file).use { input ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val count = input.read(buffer)
                if (count < 0) break
                digest.update(buffer, 0, count)
            }
        }
        return digest.digest().joinToString("") { "%02x".format(it) }
    }

    private fun buildVoice(
        folder: File,
        json: JSONObject,
        modelId: String,
        voiceId: String,
        name: String,
        speakerId: Int,
    ): LocalTtsVoice =
        LocalTtsVoice(
            modelId = modelId,
            voiceId = voiceId,
            displayName = name,
            modelRoot = folder,
            modelFile = json.optString("model", "model.int8.onnx"),
            voicesFile = json.optString("voices", "voices.bin"),
            tokensFile = json.optString("tokens", "tokens.txt"),
            lexiconFile = resolveCommaPaths(folder, json.optString("lexicon")),
            dataDirectory = resolvePath(folder, json.optString("dataDir")),
            ruleFsts = resolveCommaPaths(folder, json.optString("ruleFsts")),
            ruleFars = resolveCommaPaths(folder, json.optString("ruleFars")),
            speakerId = speakerId,
            defaultSpeed = json.optDouble("speed", 1.0).toFloat(),
        )

    private fun resolveCommaPaths(folder: File, value: String): String =
        value.split(',').map(String::trim).filter(String::isNotBlank).joinToString(",") { resolvePath(folder, it) }

    private fun resolvePath(folder: File, value: String): String {
        if (value.isBlank()) return ""
        val file = File(value)
        return if (file.isAbsolute) file.absolutePath else File(folder, value).absolutePath
    }

    companion object {
        const val MODEL_MANIFEST_SCHEMA = "click.android.local_tts_model.v1"
        const val KOKORO_ARCHIVE_SHA256 = "a422a872b27199094a32d08072b6376b0c943d392bb4f396d8a48c52594fb05a"
        private const val MAX_ARCHIVE_BYTES = 256L * 1024L * 1024L
        private const val MAX_EXPANDED_BYTES = 512L * 1024L * 1024L
        private const val MAX_FILE_COUNT = 20_000
        private val MODEL_NOTICE_FILES = listOf(
            "kokoro-model-source.txt",
            "kokoro-model-Apache-2.0.txt",
            "espeak-ng-GPL-3.0.txt",
        )
        private val verifiedModelPaths = java.util.Collections.synchronizedSet(mutableSetOf<String>())
    }
}
