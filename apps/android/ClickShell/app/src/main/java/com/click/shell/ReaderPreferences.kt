package com.click.shell

import android.content.Context
import org.readium.r2.navigator.epub.EpubPreferences
import org.readium.r2.navigator.preferences.Color as ReadiumColor
import org.readium.r2.navigator.preferences.ColumnCount
import org.readium.r2.navigator.preferences.FontFamily
import org.readium.r2.navigator.preferences.Spread
import org.readium.r2.navigator.preferences.Theme
import org.readium.r2.shared.ExperimentalReadiumApi

enum class ReaderTheme {
    DAY,
    NIGHT,
}

enum class ReaderTtsMode {
    LOCAL_PRIVATE,
    SMART,
    MICROSOFT_ONLINE,
}

enum class ChineseScriptMode {
    ORIGINAL,
    SIMPLIFIED,
    TRADITIONAL,
}

enum class ComicPageMode {
    AUTO,
    SINGLE,
    DOUBLE,
}

data class ReaderPreferences(
    val fontFamilyPreference: String = MICROSOFT_YAHEI,
    val fontSizeSp: Int = 23,
    val fontWeightPercent: Int = 100,
    val lineHeightPercent: Int = 120,
    val paragraphGapPercent: Int = 22,
    val letterSpacingPercent: Int = 0,
    val sideMarginDp: Int = 4,
    val viewportTopDp: Int = 0,
    val viewportBottomDp: Int = 0,
    val viewportLeftDp: Int = 4,
    val viewportRightDp: Int = 4,
    val theme: ReaderTheme = ReaderTheme.NIGHT,
    val showProgress: Boolean = true,
    val ttsRatePercent: Int = 100,
    val ttsTimerMinutes: Int = 60,
    val ttsMode: ReaderTtsMode = ReaderTtsMode.LOCAL_PRIVATE,
    val ttsLocalModelId: String = DEFAULT_LOCAL_MODEL,
    val ttsLocalVoiceId: String = DEFAULT_LOCAL_VOICE,
    val chineseScriptMode: ChineseScriptMode = ChineseScriptMode.ORIGINAL,
    val comicPageMode: ComicPageMode = ComicPageMode.AUTO,
) {
    companion object {
        const val MICROSOFT_YAHEI = "MicrosoftYaHeiPriority"
        const val DEFAULT_LOCAL_MODEL = "kokoro-int8-multi-lang-v1_1"
        const val DEFAULT_LOCAL_VOICE = "kokoro-zm-009"
    }
}

class ReaderPreferencesRepository(context: Context) {
    private val appContext = context.applicationContext
    private val storage = appContext.getSharedPreferences(STORAGE_NAME, Context.MODE_PRIVATE)
    private val fontManager = ReaderFontManager(appContext)
    private var activeBookId: String = ""

    fun bindBook(bookId: String): ReaderPreferencesRepository {
        activeBookId = bookId.trim().replace(Regex("[^A-Za-z0-9._-]"), "_")
        return this
    }

    private fun bookKey(base: String): String = if (activeBookId.isBlank()) base else "$base.$activeBookId"

    fun load(): ReaderPreferences {
        // Click opens in the user's preferred black reading surface on every new install.
        // An explicit day-mode choice is still persisted and respected afterwards.
        val firstInstallTheme = ReaderTheme.NIGHT
        val legacySideMargin = storage.getInt(KEY_SIDE_MARGIN, 4).coerceIn(4, 40)
        return ReaderPreferences(
            fontFamilyPreference = storage.getString(KEY_FONT_FAMILY, ReaderPreferences.MICROSOFT_YAHEI)
                ?.let { if (it == "Microsoft YaHei") ReaderPreferences.MICROSOFT_YAHEI else it }
                ?: ReaderPreferences.MICROSOFT_YAHEI,
            fontSizeSp = storage.getInt(KEY_FONT_SIZE, 23).coerceIn(16, 36),
            fontWeightPercent = storage.getInt(KEY_FONT_WEIGHT, 100).coerceIn(80, 160),
            lineHeightPercent = storage.getInt(KEY_LINE_HEIGHT, 120).coerceIn(100, 200),
            paragraphGapPercent = storage.getInt(KEY_PARAGRAPH_GAP, 22).coerceIn(0, 100),
            letterSpacingPercent = storage.getInt(KEY_LETTER_SPACING, 0).coerceIn(0, 30),
            sideMarginDp = legacySideMargin,
            viewportTopDp = storage.getInt(KEY_VIEWPORT_TOP, 0).coerceIn(0, 48),
            viewportBottomDp = storage.getInt(KEY_VIEWPORT_BOTTOM, 0).coerceIn(0, 48),
            viewportLeftDp = storage.getInt(KEY_VIEWPORT_LEFT, legacySideMargin).coerceIn(0, 48),
            viewportRightDp = storage.getInt(KEY_VIEWPORT_RIGHT, legacySideMargin).coerceIn(0, 48),
            theme = storage.getString(KEY_THEME, null)?.let {
                runCatching { ReaderTheme.valueOf(it) }.getOrNull()
            } ?: firstInstallTheme,
            showProgress = storage.getBoolean(KEY_SHOW_PROGRESS, true),
            ttsRatePercent = storage.getInt(KEY_TTS_RATE, 100).coerceIn(80, 150),
            ttsTimerMinutes = storage.getInt(KEY_TTS_TIMER, 60).takeIf { it in TIMER_VALUES } ?: 60,
            ttsMode = storage.getString(KEY_TTS_MODE, null)?.let {
                runCatching { ReaderTtsMode.valueOf(it) }.getOrNull()
            } ?: ReaderTtsMode.LOCAL_PRIVATE,
            ttsLocalModelId = storage.getString(KEY_TTS_MODEL, ReaderPreferences.DEFAULT_LOCAL_MODEL)
                ?: ReaderPreferences.DEFAULT_LOCAL_MODEL,
            ttsLocalVoiceId = storage.getString(KEY_TTS_VOICE, ReaderPreferences.DEFAULT_LOCAL_VOICE)
                ?: ReaderPreferences.DEFAULT_LOCAL_VOICE,
            chineseScriptMode = storage.getString(bookKey(KEY_CHINESE_SCRIPT_MODE), null)?.let {
                runCatching { ChineseScriptMode.valueOf(it) }.getOrNull()
            } ?: ChineseScriptMode.ORIGINAL,
            comicPageMode = storage.getString(bookKey(KEY_COMIC_PAGE_MODE), null)?.let {
                runCatching { ComicPageMode.valueOf(it) }.getOrNull()
            } ?: ComicPageMode.AUTO,
        )
    }

    fun save(value: ReaderPreferences): ReaderPreferences {
        val safe = value.copy(
            fontSizeSp = value.fontSizeSp.coerceIn(16, 36),
            fontWeightPercent = value.fontWeightPercent.coerceIn(80, 160),
            lineHeightPercent = value.lineHeightPercent.coerceIn(100, 200),
            paragraphGapPercent = value.paragraphGapPercent.coerceIn(0, 100),
            letterSpacingPercent = value.letterSpacingPercent.coerceIn(0, 30),
            sideMarginDp = value.sideMarginDp.coerceIn(4, 40),
            viewportTopDp = value.viewportTopDp.coerceIn(0, 48),
            viewportBottomDp = value.viewportBottomDp.coerceIn(0, 48),
            viewportLeftDp = value.viewportLeftDp.coerceIn(0, 48),
            viewportRightDp = value.viewportRightDp.coerceIn(0, 48),
            ttsRatePercent = value.ttsRatePercent.coerceIn(80, 150),
            ttsTimerMinutes = value.ttsTimerMinutes.takeIf { it in TIMER_VALUES } ?: 60,
        )
        storage.edit()
            .putString(KEY_FONT_FAMILY, safe.fontFamilyPreference)
            .putInt(KEY_FONT_SIZE, safe.fontSizeSp)
            .putInt(KEY_FONT_WEIGHT, safe.fontWeightPercent)
            .putInt(KEY_LINE_HEIGHT, safe.lineHeightPercent)
            .putInt(KEY_PARAGRAPH_GAP, safe.paragraphGapPercent)
            .putInt(KEY_LETTER_SPACING, safe.letterSpacingPercent)
            .putInt(KEY_SIDE_MARGIN, safe.sideMarginDp)
            .putInt(KEY_VIEWPORT_TOP, safe.viewportTopDp)
            .putInt(KEY_VIEWPORT_BOTTOM, safe.viewportBottomDp)
            .putInt(KEY_VIEWPORT_LEFT, safe.viewportLeftDp)
            .putInt(KEY_VIEWPORT_RIGHT, safe.viewportRightDp)
            .putString(KEY_THEME, safe.theme.name)
            .putBoolean(KEY_SHOW_PROGRESS, safe.showProgress)
            .putInt(KEY_TTS_RATE, safe.ttsRatePercent)
            .putInt(KEY_TTS_TIMER, safe.ttsTimerMinutes)
            .putString(KEY_TTS_MODE, safe.ttsMode.name)
            .putString(KEY_TTS_MODEL, safe.ttsLocalModelId)
            .putString(KEY_TTS_VOICE, safe.ttsLocalVoiceId)
            .putString(bookKey(KEY_CHINESE_SCRIPT_MODE), safe.chineseScriptMode.name)
            .putString(bookKey(KEY_COMIC_PAGE_MODE), safe.comicPageMode.name)
            .apply()
        return safe
    }

    fun update(block: (ReaderPreferences) -> ReaderPreferences): ReaderPreferences = save(block(load()))

    fun updateFallbackAppearance(fontSizeSp: Int, lineHeightPercent: Int, sideMarginDp: Int, darkMode: Boolean): ReaderPreferences =
        save(
            load().copy(
                fontSizeSp = fontSizeSp,
                lineHeightPercent = lineHeightPercent,
                sideMarginDp = sideMarginDp,
                viewportLeftDp = sideMarginDp,
                viewportRightDp = sideMarginDp,
                theme = if (darkMode) ReaderTheme.NIGHT else ReaderTheme.DAY,
            ),
        )

    fun updateTtsTimer(minutes: Int): ReaderPreferences = save(load().copy(ttsTimerMinutes = minutes))

    fun reset(): ReaderPreferences {
        storage.edit().clear().apply()
        return load()
    }

    fun resetLayout(): ReaderPreferences {
        val current = load()
        val defaults = ReaderPreferences()
        return save(
            current.copy(
                fontFamilyPreference = defaults.fontFamilyPreference,
                fontSizeSp = defaults.fontSizeSp,
                fontWeightPercent = defaults.fontWeightPercent,
                lineHeightPercent = defaults.lineHeightPercent,
                paragraphGapPercent = defaults.paragraphGapPercent,
                letterSpacingPercent = defaults.letterSpacingPercent,
                sideMarginDp = defaults.sideMarginDp,
                viewportTopDp = defaults.viewportTopDp,
                viewportBottomDp = defaults.viewportBottomDp,
                viewportLeftDp = defaults.viewportLeftDp,
                viewportRightDp = defaults.viewportRightDp,
                showProgress = defaults.showProgress,
                comicPageMode = defaults.comicPageMode,
            ),
        )
    }

    fun resolvedFontLabel(preferences: ReaderPreferences = load()): String =
        if (preferences.fontFamilyPreference == ReaderPreferences.MICROSOFT_YAHEI) {
            "偏好：微软雅黑 · 当前：${fontManager.resolve().resolvedLabel}"
        } else {
            preferences.fontFamilyPreference
        }

    @OptIn(ExperimentalReadiumApi::class)
    fun toEpubPreferences(
        value: ReaderPreferences = load(),
        readingProfile: String = "TEXT_REFLOW",
        isPortrait: Boolean = true,
    ): EpubPreferences {
        val isNight = value.theme == ReaderTheme.NIGHT
        val imagePublication = readingProfile == "IMAGE_COMIC" || readingProfile == "FIXED_LAYOUT"
        val useDoublePage = imagePublication && when (value.comicPageMode) {
            ComicPageMode.SINGLE -> false
            ComicPageMode.DOUBLE -> true
            ComicPageMode.AUTO -> !isPortrait
        }
        return EpubPreferences(
            backgroundColor = ReadiumColor(if (isNight) 0xFF10110F.toInt() else 0xFFF4F1E8.toInt()),
            columnCount = if (useDoublePage) ColumnCount.TWO else ColumnCount.ONE,
            fontFamily = FontFamily(fontManager.resolve().resolvedFamily),
            fontSize = (value.fontSizeSp / 16.0).coerceIn(0.8, 2.25),
            fontWeight = (value.fontWeightPercent / 100.0).coerceIn(0.8, 1.6),
            letterSpacing = (value.letterSpacingPercent / 100.0).coerceIn(0.0, 1.0),
            lineHeight = (value.lineHeightPercent / 100.0).coerceIn(1.0, 2.0),
            // Click owns per-edge viewport spacing outside Readium so each side can be
            // adjusted independently without injecting CSS into the publication.
            pageMargins = 0.0,
            paragraphSpacing = (value.paragraphGapPercent / 100.0).coerceIn(0.0, 1.0),
            publisherStyles = imagePublication,
            scroll = false,
            spread = if (useDoublePage) Spread.ALWAYS else Spread.NEVER,
            textColor = ReadiumColor(if (isNight) 0xFFE7E8E2.toInt() else 0xFF242520.toInt()),
            textNormalization = true,
            theme = if (isNight) Theme.DARK else Theme.LIGHT,
        )
    }

    companion object {
        private const val STORAGE_NAME = "click_reader_preferences_v1"
        private const val KEY_FONT_FAMILY = "font_family"
        private const val KEY_FONT_SIZE = "font_size_sp"
        private const val KEY_FONT_WEIGHT = "font_weight_percent"
        private const val KEY_LINE_HEIGHT = "line_height_percent"
        private const val KEY_PARAGRAPH_GAP = "paragraph_gap_percent"
        private const val KEY_LETTER_SPACING = "letter_spacing_percent"
        private const val KEY_SIDE_MARGIN = "side_margin_dp"
        private const val KEY_VIEWPORT_TOP = "viewport_top_dp"
        private const val KEY_VIEWPORT_BOTTOM = "viewport_bottom_dp"
        private const val KEY_VIEWPORT_LEFT = "viewport_left_dp"
        private const val KEY_VIEWPORT_RIGHT = "viewport_right_dp"
        private const val KEY_THEME = "theme"
        private const val KEY_SHOW_PROGRESS = "show_progress"
        private const val KEY_TTS_RATE = "tts_rate_percent"
        private const val KEY_TTS_TIMER = "tts_timer_minutes"
        private const val KEY_TTS_MODE = "tts_mode"
        private const val KEY_TTS_MODEL = "tts_local_model_id"
        private const val KEY_TTS_VOICE = "tts_local_voice_id"
        private const val KEY_CHINESE_SCRIPT_MODE = "chinese_script_mode"
        private const val KEY_COMIC_PAGE_MODE = "comic_page_mode"
        private val TIMER_VALUES = setOf(30, 60, 90)
    }
}
