package com.click.shell

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import org.json.JSONObject
import java.io.File

/** Debug-only shell entry that keeps the real native reader activity non-exported. */
class ReaderUiProbeActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val baseUrl = intent.getStringExtra("base_url")
        val deviceId = intent.getStringExtra("device_id")
        val accessToken = intent.getStringExtra("access_token")
        val directBookId = intent.getStringExtra("book_id").orEmpty().trim()
        if (directBookId.isNotEmpty()) {
            val store = ClickNativeStore(this)
            val epubPath = store.bookEpubLocalPath(directBookId)
            if (epubPath.isNotBlank() && File(epubPath).isFile) {
                val resetHref = intent.getStringExtra("locator_href").orEmpty().trim()
                if (resetHref.isNotEmpty()) {
                    val progression = intent.getDoubleExtra("locator_progression", 0.0)
                        .coerceIn(0.0, 0.999999)
                    val locator = JSONObject()
                        .put("href", resetHref)
                        .put("type", "application/xhtml+xml")
                        .put("locations", JSONObject().put("progression", progression))
                    store.saveLocalPosition(
                        JSONObject()
                            .put("book_id", directBookId)
                            .put("chapter_locator", resetHref)
                            .put("page_index", 0)
                            .put("total_pages", intent.getIntExtra("locator_total_pages", 1).coerceAtLeast(1))
                            .put("locator", locator),
                    )
                }
                store.close()
                startActivity(
                    Intent(this, ClickReadiumNavigatorActivity::class.java).apply {
                        putExtra(ClickReadiumNavigatorActivity.EXTRA_BASE_URL, baseUrl)
                        putExtra(ClickReadiumNavigatorActivity.EXTRA_DEVICE_ID, deviceId)
                        putExtra(ClickReadiumNavigatorActivity.EXTRA_ACCESS_TOKEN, accessToken)
                        putExtra(ClickReadiumNavigatorActivity.EXTRA_BOOK_ID, directBookId)
                        putExtra(ClickReadiumNavigatorActivity.EXTRA_EPUB_PATH, epubPath)
                        putExtra(ClickReadiumNavigatorActivity.EXTRA_SHOW_TOC, intent.getBooleanExtra("show_toc", false))
                    },
                )
                finish()
                return
            }
            store.close()
        }
        startActivity(
            Intent(this, ClickNativeReaderActivity::class.java).apply {
                putExtra(ClickNativeReaderActivity.EXTRA_BASE_URL, baseUrl)
                putExtra(ClickNativeReaderActivity.EXTRA_DEVICE_ID, deviceId)
                putExtra(ClickNativeReaderActivity.EXTRA_ACCESS_TOKEN, accessToken)
            },
        )
        finish()
    }
}
