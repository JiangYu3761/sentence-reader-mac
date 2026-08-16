package com.click.shell

import android.content.Context
import android.graphics.Typeface
import android.net.Uri
import android.util.Base64
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream

data class ReaderFontStatus(
    val preferenceLabel: String,
    val resolvedFamily: String,
    val resolvedLabel: String,
    val importedFile: File? = null,
)

class ReaderFontManager(context: Context) {
    private val appContext = context.applicationContext
    private val fontRoot = File(appContext.filesDir, "click-reader-fonts").apply { mkdirs() }
    private val manifestFile = File(fontRoot, "font_manifest.json")

    fun resolve(): ReaderFontStatus {
        val manifest = runCatching { JSONObject(manifestFile.readText(Charsets.UTF_8)) }.getOrNull()
        val file = manifest?.optString("file_name")?.takeIf(String::isNotBlank)?.let { File(fontRoot, it) }
        if (
            file?.isFile == true &&
            file.length() <= MAX_INJECTABLE_FONT_BYTES &&
            runCatching { Typeface.createFromFile(file) }.isSuccess
        ) {
            return ReaderFontStatus(
                preferenceLabel = "微软雅黑优先",
                resolvedFamily = IMPORTED_FONT_FAMILY,
                resolvedLabel = "用户导入 · ${manifest.optString("display_name", file.name)}",
                importedFile = file,
            )
        }
        if (file?.isFile == true) {
            return ReaderFontStatus(
                preferenceLabel = "微软雅黑优先",
                resolvedFamily = "sans-serif",
                resolvedLabel = "已导入 ${file.name}，但文件过大未启用",
                importedFile = file,
            )
        }
        return ReaderFontStatus(
            preferenceLabel = "微软雅黑优先",
            resolvedFamily = "sans-serif",
            resolvedLabel = "系统中文字体 · 未随 APK 提供微软雅黑",
        )
    }

    fun importFont(uri: Uri): ReaderFontStatus {
        val displayName = queryDisplayName(uri).ifBlank { "imported-font.ttf" }
        val extension = displayName.substringAfterLast('.', "").lowercase()
        require(extension in SUPPORTED_EXTENSIONS) { "只支持 ttf / otf / ttc 字体" }
        val target = File(fontRoot, "imported.$extension")
        val pending = File(fontRoot, "imported.$extension.partial")
        var total = 0L
        appContext.contentResolver.openInputStream(uri).use { input ->
            requireNotNull(input) { "无法打开字体文件" }
            FileOutputStream(pending).use { output ->
                val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                while (true) {
                    val count = input.read(buffer)
                    if (count < 0) break
                    total += count
                    require(total <= MAX_FONT_BYTES) { "字体文件超过 ${MAX_FONT_BYTES / 1024 / 1024} MB 限制" }
                    output.write(buffer, 0, count)
                }
            }
        }
        require(total > 512) { "字体文件无效" }
        runCatching { Typeface.createFromFile(pending) }.getOrElse {
            pending.delete()
            error("Android 无法解析这个字体文件")
        }
        fontRoot.listFiles()?.filter { it.name.startsWith("imported.") && it != pending }?.forEach(File::delete)
        if (target.exists()) target.delete()
        check(pending.renameTo(target)) { "字体文件保存失败" }
        manifestFile.writeText(
            JSONObject()
                .put("schema", FONT_MANIFEST_SCHEMA)
                .put("file_name", target.name)
                .put("display_name", displayName)
                .put("local_only", true)
                .toString(2),
            Charsets.UTF_8,
        )
        return resolve()
    }

    fun removeImportedFont(): Boolean {
        val current = resolve().importedFile ?: return false
        val removed = current.delete()
        manifestFile.delete()
        return removed
    }

    fun readiumInjectionScript(): String? {
        val status = resolve()
        val font = status.importedFile ?: return null
        if (font.length() > MAX_INJECTABLE_FONT_BYTES) return null
        val mime = when (font.extension.lowercase()) {
            "otf" -> "font/otf"
            "ttc" -> "font/collection"
            else -> "font/ttf"
        }
        val encoded = Base64.encodeToString(font.readBytes(), Base64.NO_WRAP)
        return """
            (function(){
              var id='click-imported-reader-font';
              var style=document.getElementById(id);
              if(!style){style=document.createElement('style');style.id=id;document.head.appendChild(style);}
              style.textContent="@font-face{font-family:'$IMPORTED_FONT_FAMILY';src:url(data:$mime;base64,$encoded)} html,body,body *{font-family:'$IMPORTED_FONT_FAMILY',sans-serif!important;}";
              return true;
            })();
        """.trimIndent()
    }

    private fun queryDisplayName(uri: Uri): String {
        val projection = arrayOf(android.provider.OpenableColumns.DISPLAY_NAME)
        return appContext.contentResolver.query(uri, projection, null, null, null)?.use { cursor ->
            if (cursor.moveToFirst()) cursor.getString(0).orEmpty() else ""
        }.orEmpty()
    }

    companion object {
        const val FONT_MANIFEST_SCHEMA = "click.android.reader_font.v1"
        const val IMPORTED_FONT_FAMILY = "ClickImportedFont"
        private val SUPPORTED_EXTENSIONS = setOf("ttf", "otf", "ttc")
        private const val MAX_FONT_BYTES = 48L * 1024L * 1024L
        private const val MAX_INJECTABLE_FONT_BYTES = 24L * 1024L * 1024L
    }
}
