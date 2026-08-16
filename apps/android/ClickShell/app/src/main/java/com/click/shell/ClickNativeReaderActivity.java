package com.click.shell;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.media.MediaPlayer;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Bundle;
import android.util.Base64;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.JavascriptInterface;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.GridLayout;
import android.widget.HorizontalScrollView;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.PopupWindow;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.SeekBar;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import androidx.core.content.FileProvider;

import coil.ImageLoader;
import coil.request.ImageRequest;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class ClickNativeReaderActivity extends Activity {
    static final String EXTRA_BASE_URL = "base_url";
    static final String EXTRA_DEVICE_ID = "device_id";
    static final String EXTRA_ACCESS_TOKEN = "access_token";
    static final String EXTRA_OPEN_SYNC_ISSUES = "open_sync_issues";
    static final String EXTRA_IMPORTED_BOOK_ID = "imported_book_id";
    static final String EXTRA_IMPORTED_SOURCE_KIND = "imported_source_kind";

    private static final int REQUEST_AUDIO_NOTE = 2101;
    private static final int REQUEST_BOOK_IMPORT = 2102;
    private static final String READIUM_TOOLKIT_DEPENDENCY = "org.readium.kotlin-toolkit";
    private static final String PDF_READER_CLASS = "com.click.shell.ClickPdfReaderActivity";
    private static final String PDF_EXTRA_BOOK_ID = "book_id";
    private static final String PDF_EXTRA_PRIVATE_PATH = "private_pdf_path";
    private static final String PDF_EXTRA_BOOK_TITLE = "book_title";

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private ClickNativeStore store;
    private ReaderAudioNoteRepository audioNoteRepository;
    private ReaderPreferencesRepository preferencesRepository;
    private ReaderPreferences readerPreferences;
    private ImageLoader coverImageLoader;
    private MediaPlayer voiceNotePlayer;
    private String playingVoiceNoteId = "";
    private LinearLayout root;
    private WebView readerWebView;
    private String baseUrl = "";
    private String deviceId = "";
    private String accessToken = "";
    private String currentBookId = "";
    private String currentBookTitle = "";
    private String currentChapterLocator = "";
    private int currentChapterIndex = 0;
    private float swipeStartX;
    private float swipeStartY;
    private long swipeStartAt;
    private String pendingAudioNoteSourceText = "";
    private List<ClickNativeStore.ChapterRow> currentChapters = new ArrayList<>();
    private String currentPageSpeakText = "";
    private int ttsTimerMinutes = 60;
    private volatile boolean syncInProgress;
    private String syncStage = "就绪";
    private String lastSyncSummary = "尚未完成本次同步";
    private int visualFontSize = 22;
    private int visualLineHeightPercent = 134;
    private int visualMargin = 14;
    private boolean visualDarkMode = true;
    private String libraryQuery = "";
    private String librarySort = "recent";
    private String libraryReadingState = "all";
    private String libraryMode = "all";
    private String libraryCategory = "";
    private String libraryAuthor = "";
    private boolean libraryVisible;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        store = new ClickNativeStore(this);
        audioNoteRepository = new ReaderAudioNoteRepository(this);
        coverImageLoader = new ImageLoader.Builder(this).crossfade(true).build();
        preferencesRepository = new ReaderPreferencesRepository(this);
        readerPreferences = preferencesRepository.load();
        visualFontSize = readerPreferences.getFontSizeSp();
        visualLineHeightPercent = readerPreferences.getLineHeightPercent();
        visualMargin = readerPreferences.getSideMarginDp();
        visualDarkMode = readerPreferences.getTheme() == ReaderTheme.NIGHT;
        ttsTimerMinutes = readerPreferences.getTtsTimerMinutes();
        Intent intent = getIntent();
        baseUrl = cleanBaseUrl(intent.getStringExtra(EXTRA_BASE_URL));
        deviceId = safe(intent.getStringExtra(EXTRA_DEVICE_ID));
        accessToken = safe(intent.getStringExtra(EXTRA_ACCESS_TOKEN));
        root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(ClickUi.canvas(this, visualDarkMode));
        ClickUi.applySystemBars(this, visualDarkMode);
        setContentView(root);
        lastSyncSummary = initialLibrarySummary(
                hasRemoteEndpoint(),
                hasInternetCapability()
        );
        renderLibrary(lastSyncSummary);
        recoverLocalFileCleanupAsync();
        if (intent.getBooleanExtra(EXTRA_OPEN_SYNC_ISSUES, false)) {
            showSyncIssuesCenter();
        } else {
            routeImportedBook(
                    intent.getStringExtra(EXTRA_IMPORTED_BOOK_ID),
                    intent.getStringExtra(EXTRA_IMPORTED_SOURCE_KIND)
            );
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        ClickSyncScheduler.enqueueMetadata(this);
        if (libraryVisible && root != null && store != null) {
            renderLibrary(lastSyncSummary);
        }
    }

    @Override
    protected void onDestroy() {
        executor.shutdownNow();
        if (readerWebView != null) {
            readerWebView.destroy();
            readerWebView = null;
        }
        if (store != null) {
            store.close();
        }
        if (coverImageLoader != null) {
            coverImageLoader.shutdown();
            coverImageLoader = null;
        }
        releaseVoiceNotePlayer();
        super.onDestroy();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_BOOK_IMPORT) {
            if (resultCode == RESULT_OK && data != null) {
                String bookId = data.getStringExtra(BookImportActivity.EXTRA_LOCAL_BOOK_ID);
                String sourceKind = data.getStringExtra(BookImportActivity.EXTRA_SOURCE_KIND);
                renderLibrary("原书已保存在手机，等待归入 Mac 主库");
                routeImportedBook(bookId, sourceKind);
            }
            return;
        }
        if (requestCode != REQUEST_AUDIO_NOTE || resultCode != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        Uri audioUri = data.getData();
        executor.execute(() -> {
            ReaderAudioNoteRepository.SavedAudio localAudio = null;
            try {
                byte[] bytes = readAllBytes(audioUri);
                if (bytes.length == 0) {
                    throw new IllegalStateException("empty audio note");
                }
                String mime = mimeType(audioUri);
                localAudio = audioNoteRepository.save(bytes, mime);
                String opId = operationId();
                JSONObject uploadMetadata = androidMetadata("audio_note");
                JSONObject payload = new JSONObject();
                payload.put("book_id", currentBookId);
                payload.put("audio_base64", Base64.encodeToString(bytes, Base64.NO_WRAP));
                payload.put("mime_type", mime);
                payload.put("source_text", pendingAudioNoteSourceText);
                payload.put("chapter_locator", currentChapterLocator);
                payload.put("metadata", uploadMetadata);
                JSONObject localAnnotation = new JSONObject(payload.toString())
                        .put("kind", "audio_note")
                        .put("note_text", "语音备注待转写")
                        .put("color", "")
                        .put(
                                "metadata",
                                audioNoteRepository.metadata(uploadMetadata, localAudio)
                        );
                store.saveLocalAnnotationAndEnqueue(
                        opId,
                        "audio_note_created",
                        currentBookId,
                        localAnnotation,
                        payload
                );
                ClickSyncScheduler.enqueueMetadata(this);
                toast("语音备注已保存，联网时会同步");
            } catch (Throwable throwable) {
                audioNoteRepository.delete(localAudio);
                toast("语音备注保存失败：" + compact(throwable));
            }
        });
    }

    private String libraryTitle() {
        if (!libraryQuery.isEmpty()) return "搜索 · " + libraryQuery;
        if ("category".equals(libraryMode) && !libraryCategory.isEmpty()) {
            return "文件夹 · " + libraryCategory;
        }
        if ("author".equals(libraryMode) && !libraryAuthor.isEmpty()) {
            return "作者 · " + libraryAuthor;
        }
        return "书架";
    }

    private void renderLibrary(String status) {
        runOnUiThread(() -> {
            libraryVisible = true;
            root.removeAllViews();
            root.setBackgroundColor(ClickUi.canvas(this, visualDarkMode));
            ClickUi.applySystemBars(this, visualDarkMode);
            syncStage = safe(status).isEmpty() ? syncStage : status;

            LinearLayout header = moonToolbar();
            header.addView(toolbarIconButton(
                    R.drawable.ic_click_menu,
                    "打开书架菜单",
                    v -> showLibraryDrawer()
            ));
            String shelfTitle = libraryTitle();
            TextView title = text(shelfTitle, 28, ClickUi.primaryLabel(this, visualDarkMode));
            ClickUi.styleNavigationTitle(title, visualDarkMode);
            title.setTextSize(28);
            title.setPadding(dp(8), 0, 0, 0);
            title.setSingleLine(true);
            title.setEllipsize(android.text.TextUtils.TruncateAt.END);
            header.addView(title, weightParams());
            header.addView(toolbarIconButton(
                    R.drawable.ic_click_search,
                    "搜索书架",
                    v -> showLibrarySearchDialog()
            ));
            Button more = toolbarIconButton(
                    R.drawable.ic_click_more,
                    "更多书架操作",
                    v -> showLibraryMoreDialog()
            );
            header.addView(more);
            root.addView(header, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(64)));

            HorizontalScrollView modeScroller = new HorizontalScrollView(this);
            modeScroller.setHorizontalScrollBarEnabled(false);
            modeScroller.setBackgroundColor(ClickUi.canvas(this, visualDarkMode));
            LinearLayout modes = new LinearLayout(this);
            modes.setOrientation(LinearLayout.HORIZONTAL);
            modes.setGravity(Gravity.CENTER_VERTICAL);
            modes.setPadding(dp(12), dp(5), dp(12), dp(5));
            modes.addView(libraryChip("全部",
                    "all".equals(libraryMode) && libraryCategory.isEmpty() && libraryAuthor.isEmpty(),
                    v -> {
                        libraryMode = "all";
                        libraryCategory = "";
                        libraryAuthor = "";
                        renderLibrary("全部书籍");
                    }
            ));
            modes.addView(libraryChip("最近", "recent".equals(libraryMode), v -> {
                libraryMode = "recent";
                libraryCategory = "";
                libraryAuthor = "";
                renderLibrary("最近阅读");
            }));
            modes.addView(libraryChip("收藏", "favorites".equals(libraryMode), v -> {
                libraryMode = "favorites";
                libraryCategory = "";
                libraryAuthor = "";
                renderLibrary("我的收藏");
            }));
            modes.addView(libraryChip(
                    "分组",
                    "category".equals(libraryMode) || "author".equals(libraryMode),
                    v -> showLibraryGroupsDialog()
            ));
            modeScroller.addView(modes);
            root.addView(modeScroller, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52)));

            int activeSyncIssues = store.syncIssueCount();
            if (syncInProgress || activeSyncIssues > 0) {
                LinearLayout syncStrip = new LinearLayout(this);
                syncStrip.setOrientation(LinearLayout.HORIZONTAL);
                syncStrip.setGravity(Gravity.CENTER_VERTICAL);
                syncStrip.setPadding(dp(14), dp(4), dp(14), dp(4));
                syncStrip.setBackgroundColor(ClickUi.surface(this, visualDarkMode));
                if (syncInProgress) {
                    ProgressBar progress = new ProgressBar(this);
                    progress.setIndeterminate(true);
                    syncStrip.addView(progress, new LinearLayout.LayoutParams(dp(20), dp(20)));
                }
                String attentionText = syncInProgress
                        ? syncStatusText()
                        : "同步问题 · " + activeSyncIssues + "，点按处理";
                TextView statusText = smallText(attentionText, 12, ClickUi.warning(this, visualDarkMode));
                statusText.setPadding(syncInProgress ? dp(8) : 0, 0, 0, 0);
                syncStrip.addView(statusText, weightWrapParams());
                if (!syncInProgress) {
                    syncStrip.setOnClickListener(v -> showSyncIssuesCenter());
                }
                root.addView(syncStrip, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(38)));
            }

            ScrollView scroll = new ScrollView(this);
            scroll.setBackgroundColor(ClickUi.canvas(this, visualDarkMode));
            LinearLayout shelf = new LinearLayout(this);
            shelf.setOrientation(LinearLayout.VERTICAL);
            shelf.setPadding(dp(8), dp(10), dp(8), dp(72));
            List<ClickNativeStore.BookRow> books = visibleBooks();
            if (books.isEmpty()) {
                TextView empty = text(emptyLibraryMessage(), 16, ClickUi.secondaryLabel(this, visualDarkMode));
                empty.setGravity(Gravity.CENTER);
                empty.setPadding(dp(20), dp(64), dp(20), 0);
                shelf.addView(empty, matchWrap());
            } else {
                GridLayout grid = new GridLayout(this);
                grid.setColumnCount(3);
                grid.setUseDefaultMargins(false);
                grid.setPadding(0, 0, 0, dp(12));
                for (ClickNativeStore.BookRow book : books) {
                    grid.addView(bookTile(book), bookTileParams());
                }
                shelf.addView(grid, matchWrap());
            }
            scroll.addView(shelf);
            root.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        });
    }

    private View bookTile(ClickNativeStore.BookRow book) {
        LinearLayout tile = new LinearLayout(this);
        tile.setOrientation(LinearLayout.VERTICAL);
        tile.setGravity(Gravity.CENTER_HORIZONTAL);
        tile.setPadding(dp(4), dp(6), dp(4), dp(10));

        FrameLayout coverFrame = new FrameLayout(this);
        coverFrame.setBackground(ClickUi.rounded(
                ClickUi.surface(this, visualDarkMode),
                dp(14),
                ClickUi.separator(this, visualDarkMode),
                1
        ));
        coverFrame.setClipToOutline(true);
        coverFrame.setElevation(dp(2));
        TextView fallback = text(coverLabel(book.title), 15, ClickUi.secondaryLabel(this, visualDarkMode));
        fallback.setGravity(Gravity.CENTER);
        fallback.setTypeface(Typeface.DEFAULT_BOLD);
        fallback.setPadding(dp(8), dp(8), dp(8), dp(8));
        coverFrame.addView(fallback, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        if (!book.coverLocalPath.isEmpty() && new File(book.coverLocalPath).isFile()) {
            ImageView cover = new ImageView(this);
            cover.setScaleType(ImageView.ScaleType.CENTER_CROP);
            cover.setContentDescription(book.title + " 封面");
            coverFrame.addView(cover, new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
            ImageRequest request = new ImageRequest.Builder(this)
                    .data(new File(book.coverLocalPath))
                    .target(cover)
                    .build();
            coverImageLoader.enqueue(request);
        }
        if ("pdf".equals(book.sourceKind)) {
            TextView typeBadge = smallText("PDF", 9, ClickUi.primaryLabel(this, true));
            typeBadge.setPadding(dp(5), dp(1), dp(5), dp(1));
            typeBadge.setBackground(rounded(
                    Color.argb(185, 12, 13, 15),
                    dp(9),
                    Color.TRANSPARENT,
                    0
            ));
            FrameLayout.LayoutParams typeParams = new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    Gravity.TOP | Gravity.START
            );
            typeParams.setMargins(dp(5), dp(5), 0, 0);
            coverFrame.addView(typeBadge, typeParams);
        }
        String offlineState = bookOfflineState(book);
        if (!"可离线".equals(offlineState)) {
            TextView offlineBadge = smallText(
                    offlineState,
                    9,
                    "同步失败".equals(offlineState)
                            ? ClickUi.destructive(this, visualDarkMode)
                            : ClickUi.warning(this, visualDarkMode)
            );
            offlineBadge.setPadding(dp(6), dp(2), dp(6), dp(2));
            offlineBadge.setBackground(rounded(Color.argb(205, 12, 13, 15), dp(10), Color.TRANSPARENT, 0));
            FrameLayout.LayoutParams offlineParams = new FrameLayout.LayoutParams(
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT,
                    Gravity.BOTTOM | Gravity.START
            );
            offlineParams.setMargins(dp(5), 0, 0, dp(5));
            coverFrame.addView(offlineBadge, offlineParams);
        }
        coverFrame.setOnClickListener(v -> openBook(book.id, true));
        coverFrame.setOnLongClickListener(v -> {
            v.performHapticFeedback(android.view.HapticFeedbackConstants.LONG_PRESS);
            showBookMoreDialog(book);
            return true;
        });
        LinearLayout.LayoutParams coverParams = new LinearLayout.LayoutParams(dp(96), dp(142));
        coverParams.setMargins(0, 0, 0, dp(6));
        tile.addView(coverFrame, coverParams);

        TextView title = text((book.favorite ? "★ " : "") + book.title, 14, ClickUi.primaryLabel(this, visualDarkMode));
        title.setTypeface(Typeface.DEFAULT_BOLD);
        title.setMaxLines(2);
        title.setEllipsize(android.text.TextUtils.TruncateAt.END);
        title.setGravity(Gravity.CENTER);
        tile.addView(title, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(38)));

        String authorOrCategory = !book.customCategory.isEmpty() ? book.customCategory : book.author;
        TextView state = smallText(authorOrCategory, 12, ClickUi.secondaryLabel(this, visualDarkMode));
        state.setGravity(Gravity.CENTER);
        state.setSingleLine(true);
        state.setEllipsize(android.text.TextUtils.TruncateAt.END);
        tile.addView(state, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(18)));
        return tile;
    }

    private String bookOfflineState(ClickNativeStore.BookRow book) {
        if (ClickNativeStore.IMPORT_PENDING.equals(book.importState)) {
            return "待归档";
        }
        if (ClickNativeStore.IMPORT_TERMINAL.equals(book.importState)) {
            return "同步失败";
        }
        if (!book.assetIssueSummary.isEmpty()) {
            return "同步失败";
        }
        return localBookFile(book) == null ? "需下载" : "可离线";
    }

    private void openBook(String bookId, boolean continueReading) {
        ClickNativeStore.BookRow book = findBook(bookId);
        if (book == null) {
            toast("这本书已经不在手机书架");
            return;
        }
        if ("pdf".equals(book.sourceKind)) {
            openPdfNavigator(book);
            return;
        }
        if (openReadiumNavigator(bookId, !continueReading)) {
            store.markBookOpened(bookId);
            return;
        }
        if (!prepareBookForOpening(bookId)) {
            return;
        }
        if (continueReading) {
            openChapter(0);
        } else {
            renderToc();
        }
    }

    private void openBookAtLocator(String bookId, String targetLocator) {
        ClickNativeStore.BookRow book = findBook(bookId);
        if (book == null) {
            toast("这本书已经不在手机书架");
            return;
        }
        if ("pdf".equals(book.sourceKind)) {
            openPdfNavigator(book);
            return;
        }
        String locator = safe(targetLocator).trim();
        if (locator.isEmpty()) {
            openBook(bookId, true);
            return;
        }
        if (openReadiumNavigator(bookId, false, locator)) {
            store.markBookOpened(bookId);
            return;
        }
        if (!prepareBookForOpening(bookId)) {
            return;
        }
        for (int index = 0; index < currentChapters.size(); index++) {
            if (locator.equals(currentChapters.get(index).locator)) {
                openChapter(index);
                return;
            }
        }
        openChapter(0);
    }

    private boolean prepareBookForOpening(String bookId) {
        libraryVisible = false;
        store.markBookOpened(bookId);
        currentBookId = bookId;
        currentChapters = store.chapters(bookId);
        currentBookTitle = "";
        for (ClickNativeStore.BookRow row : store.books()) {
            if (row.id.equals(bookId)) {
                currentBookTitle = row.title;
                break;
            }
        }
        if (currentChapters.isEmpty()) {
            toast("这本书还没有缓存正文，请先同步全部");
            return false;
        }
        return true;
    }

    private boolean openReadiumNavigator(String bookId, boolean showToc) {
        return openReadiumNavigator(bookId, showToc, "");
    }

    private boolean openReadiumNavigator(String bookId, boolean showToc, String targetLocator) {
        ClickNativeStore.BookRow book = findBook(bookId);
        if (book == null || !"epub".equals(book.sourceKind)) {
            return false;
        }
        File file = localBookFile(book);
        if (file == null) {
            return false;
        }
        Intent intent = new Intent(this, ClickReadiumNavigatorActivity.class);
        intent.putExtra(ClickReadiumNavigatorActivity.EXTRA_BASE_URL, baseUrl);
        intent.putExtra(ClickReadiumNavigatorActivity.EXTRA_DEVICE_ID, deviceId);
        intent.putExtra(ClickReadiumNavigatorActivity.EXTRA_ACCESS_TOKEN, accessToken);
        intent.putExtra(ClickReadiumNavigatorActivity.EXTRA_BOOK_ID, bookId);
        intent.putExtra(ClickReadiumNavigatorActivity.EXTRA_EPUB_PATH, file.getAbsolutePath());
        intent.putExtra(ClickReadiumNavigatorActivity.EXTRA_READING_PROFILE, store.bookReadingProfile(bookId));
        intent.putExtra(ClickReadiumNavigatorActivity.EXTRA_DISPLAY_VARIANTS_PATH, store.bookDisplayVariantsLocalPath(bookId));
        intent.putExtra(ClickReadiumNavigatorActivity.EXTRA_SHOW_TOC, showToc);
        if (!safe(targetLocator).trim().isEmpty()) {
            intent.putExtra(ClickReadiumNavigatorActivity.EXTRA_TARGET_LOCATOR, targetLocator);
        }
        startActivity(intent);
        return true;
    }

    private void openPdfNavigator(ClickNativeStore.BookRow book) {
        File file = localBookFile(book);
        if (file == null) {
            toast("这份 PDF 还没有下载到手机，请先点“下载到本机”");
            return;
        }
        Intent intent = new Intent();
        intent.setClassName(this, PDF_READER_CLASS);
        intent.putExtra(PDF_EXTRA_BOOK_ID, book.id);
        intent.putExtra(PDF_EXTRA_PRIVATE_PATH, file.getAbsolutePath());
        intent.putExtra(PDF_EXTRA_BOOK_TITLE, book.title);
        try {
            startActivity(intent);
            store.markBookOpened(book.id);
        } catch (Throwable error) {
            toast("PDF 阅读器尚未接通，请稍后重试");
        }
    }

    private ClickNativeStore.BookRow findBook(String bookId) {
        String targetId = safe(bookId).trim();
        for (ClickNativeStore.BookRow row : store.books()) {
            if (row.id.equals(targetId)) {
                return row;
            }
        }
        return null;
    }

    private File localBookFile(ClickNativeStore.BookRow book) {
        if (book == null) {
            return null;
        }
        File source = appPrivateReadableFile(book.sourceLocalPath);
        if (source != null) {
            return source;
        }
        return "epub".equals(book.sourceKind)
                ? appPrivateReadableFile(book.epubLocalPath)
                : null;
    }

    private File appPrivateReadableFile(String rawPath) {
        String path = safe(rawPath).trim();
        if (path.isEmpty()) {
            return null;
        }
        try {
            File rootDirectory = getFilesDir().getCanonicalFile();
            File target = new File(path).getCanonicalFile();
            String rootPrefix = rootDirectory.getPath() + File.separator;
            return target.getPath().startsWith(rootPrefix)
                    && target.isFile()
                    && target.length() > 0L
                    ? target
                    : null;
        } catch (Throwable ignored) {
            return null;
        }
    }

    private void routeImportedBook(String rawBookId, String rawSourceKind) {
        String bookId = safe(rawBookId).trim();
        if (bookId.isEmpty()) {
            return;
        }
        ClickNativeStore.BookRow book = findBook(bookId);
        if (book == null) {
            toast("原书已保存，但书架登记还没有完成，请稍后返回书架");
            return;
        }
        String sourceKind = safe(rawSourceKind).trim().toLowerCase(java.util.Locale.ROOT);
        if (!sourceKind.isEmpty() && !sourceKind.equals(book.sourceKind)) {
            toast("原书类型校验不一致，已留在书架等待处理");
            return;
        }
        openBook(book.id, true);
    }

    private void openBookImport() {
        try {
            startActivityForResult(
                    new Intent(this, BookImportActivity.class),
                    REQUEST_BOOK_IMPORT
            );
        } catch (Throwable error) {
            toast("系统文件选择器暂时无法打开");
        }
    }

    private void renderToc() {
        runOnUiThread(() -> {
            root.removeAllViews();
            LinearLayout toolbar = compactHeader();
            toolbar.addView(pillButton("书架", v -> renderLibrary("本地缓存 " + store.books().size() + " 本")));
            TextView heading = smallText("目录", 17, Color.WHITE);
            heading.setTypeface(Typeface.DEFAULT_BOLD);
            toolbar.addView(heading, weightParams());
            toolbar.addView(pillButton("同步", v -> syncChangesThenRender()));
            root.addView(toolbar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(54)));

            ScrollView scroll = new ScrollView(this);
            LinearLayout list = new LinearLayout(this);
            list.setOrientation(LinearLayout.VERTICAL);
            list.setPadding(dp(14), dp(8), dp(14), dp(32));
            for (ClickNativeStore.ChapterRow chapter : currentChapters) {
                Button item = listButton((chapter.index + 1) + ". " + chapter.title, v -> openChapter(chapter.index));
                item.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
                LinearLayout.LayoutParams itemParams = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(52));
                itemParams.setMargins(0, 0, 0, dp(6));
                list.addView(item, itemParams);
            }
            scroll.addView(list);
            root.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        });
    }

    private void openChapter(int chapterIndex) {
        if (chapterIndex < 0 || chapterIndex >= currentChapters.size()) {
            toast(chapterIndex < 0 ? "已经是第一章" : "已经是最后一章");
            return;
        }
        ClickNativeStore.ChapterRow chapter = currentChapters.get(chapterIndex);
        currentChapterIndex = chapterIndex;
        currentChapterLocator = chapter.locator;
        currentBookTitle = currentBookTitle.isEmpty() ? currentBookId : currentBookTitle;
        String html = store.chapterHtml(currentBookId, chapterIndex);
        if (html.trim().isEmpty()) {
            html = "<p>此章节正文提取失败。请回到同一 Wi-Fi 后重新同步。</p>";
        }
        currentPageSpeakText = plainText(html);
        enqueuePosition(chapter);
        renderReader(chapter, html);
    }

    private void renderReader(ClickNativeStore.ChapterRow chapter, String chapterHtml) {
        runOnUiThread(() -> {
            root.removeAllViews();
            root.setBackgroundColor(visualDarkMode ? Color.rgb(8, 8, 7) : Color.rgb(238, 234, 222));

            readerWebView = new WebView(this);
            WebSettings settings = readerWebView.getSettings();
            settings.setJavaScriptEnabled(true);
            settings.setDomStorageEnabled(true);
            readerWebView.setBackgroundColor(visualDarkMode ? Color.rgb(8, 8, 7) : Color.rgb(238, 234, 222));
            readerWebView.addJavascriptInterface(new ReaderBridge(), "ClickNativeReader");
            readerWebView.setOnTouchListener((view, event) -> {
                if (event.getActionMasked() == MotionEvent.ACTION_DOWN) {
                    swipeStartX = event.getX();
                    swipeStartY = event.getY();
                    swipeStartAt = System.currentTimeMillis();
                    return false;
                }
                if (event.getActionMasked() == MotionEvent.ACTION_UP) {
                    float dx = event.getX() - swipeStartX;
                    float dy = event.getY() - swipeStartY;
                    long elapsed = System.currentTimeMillis() - swipeStartAt;
                    if (Math.abs(dx) > dp(86) && Math.abs(dx) > Math.abs(dy) * 1.7f && elapsed < 900) {
                        readerWebView.evaluateJavascript("window.__clickTurnPage && window.__clickTurnPage(" + (dx < 0 ? "1" : "-1") + ");", null);
                        return true;
                    }
                }
                return false;
            });
            root.addView(readerWebView, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
            String readerBaseUrl = hasRemoteEndpoint() ? baseUrl + "/" : null;
            readerWebView.loadDataWithBaseURL(readerBaseUrl, readerHtml(chapter, chapterHtml), "text/html", "UTF-8", null);
        });
    }

    private String readerHtml(ClickNativeStore.ChapterRow chapter, String bodyHtml) {
        return "<!doctype html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
                + "<style>"
                + "html,body{margin:0;padding:0;width:100%;height:100%;overflow:hidden;background:" + readerBgColor() + ";color:" + readerTextColor() + ";font-family:'Microsoft YaHei','Noto Sans CJK SC','PingFang SC',sans-serif;font-size:" + visualFontSize + "px;line-height:" + (visualLineHeightPercent / 100.0f) + ";}"
                + "#pageFrame{position:fixed;left:0;right:0;top:0;bottom:0;overflow:hidden;background:" + readerBgColor() + ";}"
                + "#content{height:calc(100vh - 28px);column-width:calc(100vw - " + (visualMargin * 2) + "px);column-gap:34px;padding:10px " + visualMargin + "px 28px;box-sizing:border-box;overflow-wrap:anywhere;}"
                + "h1,h2,h3{font-size:1.04em;line-height:1.18;margin:0 0 10px;color:" + readerTextColor() + ";}"
                + "p,li,blockquote{margin:0 0 .34em;break-inside:avoid;}"
                + "img{max-width:100%;height:auto;}"
                + ".chapter-title{font-size:12px;color:" + readerMutedColor() + ";margin-bottom:8px;}"
                + "#topChrome{position:fixed;display:none;left:0;right:0;top:0;height:52px;align-items:center;background:#dfe4f4;color:#20242c;z-index:9998;border-bottom:1px solid #c9cede;box-shadow:0 8px 24px rgba(0,0,0,.14);}"
                + "#topChrome .reader-title{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center;font-size:15px;font-weight:700;}"
                + "#topChrome button{width:56px;height:44px;border:0;background:transparent;color:#4b5262;font-size:25px;font-weight:700;}"
                + "#bottomChrome{position:fixed;display:none;left:0;right:0;bottom:0;background:#dfe4f4;color:#20242c;z-index:9998;border-top:1px solid #c9cede;box-shadow:0 -8px 24px rgba(0,0,0,.18);}"
                + "#bottomChrome .progress{display:flex;align-items:center;gap:10px;height:38px;padding:0 14px;font-size:14px;font-weight:700;}"
                + "#progressTrack{height:4px;background:#bfc7dc;flex:1;border-radius:4px;overflow:hidden;}"
                + "#progressFill{height:100%;width:0;background:#4d6aa4;}"
                + "#bottomChrome .actions{display:flex;height:58px;align-items:center;justify-content:space-around;}"
                + "#bottomChrome button{border:0;background:transparent;color:#76566d;font-size:20px;font-weight:800;width:33.333%;height:44px;}"
                + "#selectionBar{position:fixed;display:none;left:8px;right:8px;bottom:8px;background:#181818;border:1px solid #3a3a3a;border-radius:16px;padding:7px;z-index:9999;box-shadow:0 6px 22px rgba(0,0,0,.5);}"
                + "#selectionBar button{width:19.2%;margin:0 .3%;height:40px;border:0;border-radius:10px;background:#262626;color:#fff;font-size:14px;font-weight:700;}"
                + "#selectionBar button.primary{background:#1668e8;}"
                + "</style></head><body data-click-reader-ui=\"Click Android Reader P1.3 compatible immersive page\" data-single-swipe=\"singleSwipeTurnLocked\">"
                + "<div id=\"topChrome\"><button data-chrome=\"library\" aria-label=\"返回书架\">‹</button><span class=\"reader-title\">" + escapeHtml(chapter.title) + "</span><button data-chrome=\"more\" aria-label=\"更多阅读操作\">⋮</button></div>"
                + "<div id=\"pageFrame\"><main id=\"content\"><div class=\"chapter-title\">" + escapeHtml((chapter.index + 1) + "/" + currentChapters.size() + " " + chapter.title) + "</div>"
                + bodyHtml
                + "</main></div>"
                + "<div id=\"bottomChrome\"><div class=\"progress\"><span id=\"chapterLabel\">" + escapeHtml((chapter.index + 1) + "/" + currentChapters.size()) + "</span><div id=\"progressTrack\"><div id=\"progressFill\"></div></div><span id=\"pageLabel\">1/1</span></div><div class=\"actions\"><button data-chrome=\"toc\" aria-label=\"目录\">☷</button><button data-chrome=\"tts\" aria-label=\"朗读\">▶</button><button data-chrome=\"visual\" aria-label=\"显示\">Aa</button></div></div>"
                + "<div id=\"selectionBar\"><button class=\"primary\" data-action=\"copy\">复制</button><button data-action=\"red\">标红</button><button data-action=\"note\">备注</button><button data-action=\"speak\">播放</button><button data-action=\"more\">⋯</button></div>"
                + "<script>"
                + "(function(){"
                + "var bar=document.getElementById('selectionBar'),top=document.getElementById('topChrome'),chrome=document.getElementById('bottomChrome'),frame=document.getElementById('pageFrame'),content=document.getElementById('content'),fill=document.getElementById('progressFill'),label=document.getElementById('pageLabel');var selected='',page=0,total=1,singleSwipeTurnLocked=false,touchX=0,touchY=0,touchAt=0;"
                + "function text(){var s=window.getSelection?String(window.getSelection()):'';return s.replace(/\\s+/g,' ').trim();}"
                + "function show(){selected=text();bar.style.display=selected?'block':'none';}"
                + "function update(){var w=Math.max(1,frame.clientWidth);total=Math.max(1,Math.ceil(content.scrollWidth/w));page=Math.max(0,Math.min(page,total-1));frame.scrollLeft=page*w;label.textContent=(page+1)+'/'+total;fill.style.width=((page+1)/total*100)+'%';}"
                + "function turn(delta){if(singleSwipeTurnLocked)return;singleSwipeTurnLocked=true;setTimeout(function(){singleSwipeTurnLocked=false;},520);update();if(delta<0&&page<=0){ClickNativeReader.onTurnPage(-1);return;}if(delta>0&&page>=total-1){ClickNativeReader.onTurnPage(1);return;}page+=delta;frame.scrollTo({left:page*frame.clientWidth,behavior:'smooth'});setTimeout(update,260);}"
                + "function toggleChrome(){var visible=chrome.style.display!=='block';chrome.style.display=visible?'block':'none';top.style.display=visible?'flex':'none';}"
                + "document.addEventListener('selectionchange',function(){setTimeout(show,120);});"
                + "document.addEventListener('click',function(e){if(bar.contains(e.target)||chrome.contains(e.target)||top.contains(e.target))return;if(text())return;var x=e.clientX,w=window.innerWidth;if(x<w*.28){turn(-1);}else if(x>w*.72){turn(1);}else{toggleChrome();}});"
                + "document.addEventListener('touchstart',function(e){var t=e.changedTouches[0];touchX=t.clientX;touchY=t.clientY;touchAt=Date.now();},{passive:true});"
                + "document.addEventListener('touchend',function(e){var t=e.changedTouches[0],dx=t.clientX-touchX,dy=t.clientY-touchY;if(Math.abs(dx)>78&&Math.abs(dx)>Math.abs(dy)*1.65&&Date.now()-touchAt<900){turn(dx<0?1:-1);}}, {passive:true});"
                + "bar.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;selected=selected||text();if(!selected)return;ClickNativeReader.onSelectionAction(b.getAttribute('data-action'),selected);if(b.getAttribute('data-action')!=='speak'){bar.style.display='none';}});"
                + "chrome.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;ClickNativeReader.onChromeAction(b.getAttribute('data-chrome'));});"
                + "top.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;ClickNativeReader.onChromeAction(b.getAttribute('data-chrome'));});"
                + "window.__clickTurnPage=turn;window.__clickApplyVisualSetting=function(size,line,margin,dark){document.documentElement.style.fontSize=size+'px';document.documentElement.style.lineHeight=(line/100);content.style.padding='10px '+margin+'px 28px';content.style.columnWidth='calc(100vw - '+(margin*2)+'px)';document.body.style.background=dark?'#080807':'#eeeade';frame.style.background=document.body.style.background;setTimeout(update,80);};window.addEventListener('resize',function(){setTimeout(update,120);});setTimeout(update,180);})();"
                + "</script></body></html>";
    }

    private void showTtsPanel() {
        final String[] labels = {"30 分钟", "60 分钟", "90 分钟"};
        final int[] values = {30, 60, 90};
        int checked = ttsTimerMinutes == 30 ? 0 : (ttsTimerMinutes == 90 ? 2 : 1);
        new AlertDialog.Builder(this)
                .setTitle("朗读")
                .setSingleChoiceItems(labels, checked, (dialog, which) -> {
                    ttsTimerMinutes = values[which];
                    readerPreferences = preferencesRepository.updateTtsTimer(ttsTimerMinutes);
                    ReaderTtsSession.INSTANCE.setTimer(ttsTimerMinutes);
                    toast("已设置 " + ttsTimerMinutes + " 分钟后停止");
                    dialog.dismiss();
                })
                .setPositiveButton("读页", (dialog, which) -> speakText(extractSpeakableText(), "page"))
                .setNeutralButton("停止", (dialog, which) -> ReaderTtsSession.INSTANCE.stop())
                .setNegativeButton("关闭", null)
                .show();
    }

    private void showSelectionMoreDialog(String selected) {
        final String[] items = {"查词", "语音备注", "问 AI"};
        new AlertDialog.Builder(this)
                .setTitle("更多")
                .setItems(items, (dialog, which) -> {
                    if (which == 0) {
                        lookupWord(selected);
                    } else if (which == 1) {
                        chooseAudioNote(selected);
                    } else {
                        askHermes(selected);
                    }
                })
                .show();
    }

    private final class ReaderBridge {
        @JavascriptInterface
        public void onSelectionAction(String action, String text) {
            String selected = safe(text).trim();
            if (selected.isEmpty()) {
                return;
            }
            runOnUiThread(() -> {
                if ("copy".equals(action)) {
                    copyText(selected);
                } else if ("red".equals(action)) {
                    enqueueTextAnnotation("red_highlight_created", "red_highlight", selected, "", "red");
                } else if ("note".equals(action)) {
                    showNoteDialog(selected);
                } else if ("lookup".equals(action)) {
                    lookupWord(selected);
                } else if ("speak".equals(action)) {
                    speakText(selected, "selection");
                } else if ("audio_note".equals(action)) {
                    chooseAudioNote(selected);
                } else if ("hermes".equals(action)) {
                    askHermes(selected);
                } else if ("more".equals(action)) {
                    showSelectionMoreDialog(selected);
                }
            });
        }

        @JavascriptInterface
        public void onChromeAction(String action) {
            runOnUiThread(() -> {
                if ("library".equals(action)) {
                    renderLibrary("本地缓存 " + store.books().size() + " 本 · " + lastSyncSummary);
                } else if ("toc".equals(action)) {
                    showReaderTocDrawer();
                } else if ("visual".equals(action)) {
                    showVisualSettingsPanel();
                } else if ("tts".equals(action)) {
                    showTtsPanel();
                } else if ("more".equals(action)) {
                    showReaderMoreDialog();
                }
            });
        }

        @JavascriptInterface
        public void onTurnPage(int direction) {
            runOnUiThread(() -> openChapter(direction < 0 ? currentChapterIndex - 1 : currentChapterIndex + 1));
        }
    }

    private void showLibraryDrawer() {
        LinearLayout drawer = new LinearLayout(this);
        drawer.setOrientation(LinearLayout.VERTICAL);
        drawer.setPadding(dp(18), dp(28), dp(18), dp(18));
        drawer.setBackgroundColor(Color.rgb(16, 17, 19));

        TextView heading = text("我的阅读", 25, Color.rgb(244, 244, 242));
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        heading.setGravity(Gravity.CENTER_VERTICAL);
        drawer.addView(heading, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(64)));

        addDrawerAction(drawer, "◎  我的内容", v -> {
            closeParentPopup(v);
            showLibraryContentDialog();
        });
        addDrawerAction(drawer, "⌕  离线全文搜索", v -> {
            closeParentPopup(v);
            showOfflineSearchDialog();
        });
        addDrawerAction(drawer, "▦  文件夹与作者", v -> {
            closeParentPopup(v);
            showLibraryGroupsDialog();
        });

        int activeSyncIssues = store.syncIssueCount();
        if (activeSyncIssues > 0) {
            addDrawerAction(drawer, "⚠  同步问题 · " + activeSyncIssues, v -> {
                closeParentPopup(v);
                showSyncIssuesCenter();
            });
        }

        View spacer = new View(this);
        drawer.addView(spacer, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        addDrawerAction(drawer, "⚙  系统设置", v -> {
            closeParentPopup(v);
            showLibrarySettingsDialog();
        });

        PopupWindow popup = new PopupWindow(drawer, Math.round(getResources().getDisplayMetrics().widthPixels * 0.76f), ViewGroup.LayoutParams.MATCH_PARENT, true);
        popup.setOutsideTouchable(true);
        popup.showAtLocation(root, Gravity.START | Gravity.TOP, 0, 0);
        drawer.setTag(popup);
    }

    private void addDrawerAction(LinearLayout drawer, String label, View.OnClickListener listener) {
        TextView row = drawerRow(label);
        row.setOnClickListener(listener);
        drawer.addView(row, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(54)));
    }

    private void showLibraryContentDialog() {
        final String[] items = {"笔记", "标红", "语音备注"};
        new AlertDialog.Builder(this)
                .setTitle("我的内容")
                .setItems(items, (dialog, which) -> {
                    if (which == 0) {
                        showGlobalAnnotationCenter("note", "笔记中心");
                    } else if (which == 1) {
                        showGlobalAnnotationCenter("red_highlight", "标红中心");
                    } else {
                        showVoiceNotesCenter();
                    }
                })
                .setNegativeButton("关闭", null)
                .show();
    }

    private void showLibraryGroupsDialog() {
        final String[] items = {"文件夹", "作者", "整理文件夹"};
        new AlertDialog.Builder(this)
                .setTitle("分组")
                .setItems(items, (dialog, which) -> {
                    if (which == 0) {
                        showLibraryCategoryDialog();
                    } else if (which == 1) {
                        showLibraryAuthorDialog();
                    } else {
                        showFolderOrganizerDialog();
                    }
                })
                .setNegativeButton("关闭", null)
                .show();
    }

    private void showLibraryCategoryDialog() {
        java.util.TreeMap<String, Integer> counts = new java.util.TreeMap<>();
        for (ClickNativeStore.BookRow row : store.books()) {
            if (!row.customCategory.isEmpty()) counts.merge(row.customCategory, 1, Integer::sum);
        }
        if (counts.isEmpty()) {
            new AlertDialog.Builder(this)
                    .setTitle("文件夹")
                    .setMessage("还没有文件夹。创建后可以一次放入多本书。")
                    .setPositiveButton("创建文件夹", (dialog, which) -> showCreateFolderDialog())
                    .setNegativeButton("关闭", null)
                    .show();
            return;
        }
        List<String> folders = new ArrayList<>(counts.keySet());
        String[] labels = new String[folders.size()];
        for (int index = 0; index < folders.size(); index++) {
            String folder = folders.get(index);
            labels[index] = folder + " · " + counts.get(folder) + " 本";
        }
        new AlertDialog.Builder(this)
                .setTitle("文件夹")
                .setItems(labels, (dialog, which) -> {
                    libraryMode = "category";
                    libraryCategory = folders.get(which);
                    libraryAuthor = "";
                    renderLibrary("文件夹 · " + folders.get(which));
                })
                .setNeutralButton("整理", (dialog, which) -> showFolderOrganizerDialog())
                .setNegativeButton("关闭", null)
                .show();
    }

    private void showLibraryAuthorDialog() {
        java.util.TreeMap<String, Integer> counts = new java.util.TreeMap<>();
        for (ClickNativeStore.BookRow row : store.books()) {
            String author = displayAuthor(row);
            counts.merge(author, 1, Integer::sum);
        }
        if (counts.isEmpty()) {
            toast("书架里还没有书");
            return;
        }
        List<String> authors = new ArrayList<>(counts.keySet());
        String[] labels = new String[authors.size()];
        for (int index = 0; index < authors.size(); index++) {
            String author = authors.get(index);
            labels[index] = author + " · " + counts.get(author) + " 本";
        }
        new AlertDialog.Builder(this)
                .setTitle("作者")
                .setItems(labels, (dialog, which) -> {
                    libraryMode = "author";
                    libraryAuthor = authors.get(which);
                    libraryCategory = "";
                    renderLibrary("作者 · " + authors.get(which));
                })
                .setNegativeButton("关闭", null)
                .show();
    }

    private String displayAuthor(ClickNativeStore.BookRow book) {
        String author = safe(book.author).trim();
        return author.isEmpty() ? "未知作者" : author;
    }

    private void showFolderOrganizerDialog() {
        java.util.TreeMap<String, Integer> counts = new java.util.TreeMap<>();
        for (ClickNativeStore.BookRow row : store.books()) {
            if (!row.customCategory.isEmpty()) counts.merge(row.customCategory, 1, Integer::sum);
        }
        List<String> folders = new ArrayList<>(counts.keySet());
        String[] items = new String[folders.size() + 1];
        items[0] = "＋ 新建文件夹";
        for (int index = 0; index < folders.size(); index++) {
            String folder = folders.get(index);
            items[index + 1] = folder + " · " + counts.get(folder) + " 本";
        }
        new AlertDialog.Builder(this)
                .setTitle("整理文件夹")
                .setItems(items, (dialog, which) -> {
                    if (which == 0) {
                        showCreateFolderDialog();
                    } else {
                        showFolderBookPicker(folders.get(which - 1));
                    }
                })
                .setNegativeButton("关闭", null)
                .show();
    }

    private void showCreateFolderDialog() {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setHint("例如：经济学");
        input.setTextColor(Color.rgb(244, 244, 242));
        input.setHintTextColor(Color.rgb(125, 127, 132));
        new AlertDialog.Builder(this)
                .setTitle("新建文件夹")
                .setView(input)
                .setPositiveButton("下一步", (dialog, which) -> {
                    String folder = normalizedFolderName(input.getText().toString());
                    if (folder.isEmpty()) {
                        toast("请输入 1–40 个字符的文件夹名称");
                    } else {
                        showFolderBookPicker(folder);
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private String normalizedFolderName(String value) {
        String folder = safe(value).trim().replaceAll("\\s+", " ");
        if (folder.isEmpty() || folder.length() > 40) {
            return "";
        }
        return folder;
    }

    private void showFolderBookPicker(String folder) {
        List<ClickNativeStore.BookRow> books = store.books();
        if (books.isEmpty()) {
            toast("书架里还没有书");
            return;
        }
        String[] labels = new String[books.size()];
        boolean[] checked = new boolean[books.size()];
        for (int index = 0; index < books.size(); index++) {
            ClickNativeStore.BookRow book = books.get(index);
            labels[index] = folderBookLabel(book);
            checked[index] = folder.equals(book.customCategory);
        }
        new AlertDialog.Builder(this)
                .setTitle("选择放入“" + folder + "”的书")
                .setMultiChoiceItems(labels, checked, (dialog, which, selected) -> checked[which] = selected)
                .setPositiveButton("保存", (dialog, which) -> persistFolderMembership(folder, books, checked))
                .setNegativeButton("取消", null)
                .show();
    }

    private String folderBookLabel(ClickNativeStore.BookRow book) {
        String title = safe(book.title).trim().replaceAll("\\s+", " ");
        if (title.length() > 20) title = title.substring(0, 20) + "…";
        String author = displayAuthor(book);
        if (author.length() > 10) author = author.substring(0, 10) + "…";
        return title + " · " + author;
    }

    private void persistFolderMembership(
            String folder,
            List<ClickNativeStore.BookRow> books,
            boolean[] checked
    ) {
        int changed = 0;
        int selected = 0;
        try {
            for (int index = 0; index < books.size(); index++) {
                ClickNativeStore.BookRow book = books.get(index);
                if (checked[index]) selected++;
                String targetFolder = checked[index]
                        ? folder
                        : (folder.equals(book.customCategory) ? "" : book.customCategory);
                if (targetFolder.equals(book.customCategory)) continue;
                store.setBookOrganizationAndEnqueue(
                        operationId(),
                        book.id,
                        book.favorite,
                        targetFolder,
                        new JSONArray(book.tagsJson)
                );
                changed++;
            }
            if (changed > 0) ClickSyncScheduler.enqueueMetadata(this);
            if (selected > 0) {
                libraryMode = "category";
                libraryCategory = folder;
                libraryAuthor = "";
                renderLibrary(changed > 0 ? "文件夹已更新" : "文件夹没有变化");
            } else {
                libraryMode = "all";
                libraryCategory = "";
                libraryAuthor = "";
                renderLibrary(changed > 0 ? "文件夹已清空" : "没有选择书籍");
            }
        } catch (Throwable error) {
            if (changed > 0) ClickSyncScheduler.enqueueMetadata(this);
            toast("文件夹只保存了部分变更：" + compact(error));
            renderLibrary("请重新检查文件夹");
        }
    }

    private void showLibrarySettingsDialog() {
        final String themeAction = "系统外观 · " + (visualDarkMode ? "夜间" : "日间");
        final String[] items = {"阅读排版", themeAction, "下载设置"};
        new AlertDialog.Builder(this)
                .setTitle("系统设置")
                .setItems(items, (dialog, which) -> {
                    if (which == 0) {
                        showVisualSettingsPanel();
                    } else if (which == 1) {
                        showSystemAppearanceDialog();
                    } else {
                        showDownloadSettingsPanel();
                    }
                })
                .setNegativeButton("关闭", null)
                .show();
    }

    private void showSystemAppearanceDialog() {
        final String[] items = {"日间", "夜间"};
        new AlertDialog.Builder(this)
                .setTitle("系统外观")
                .setSingleChoiceItems(items, visualDarkMode ? 1 : 0, (dialog, which) -> {
                    visualDarkMode = which == 1;
                    applyReaderVisualSettings();
                    dialog.dismiss();
                    renderLibrary(visualDarkMode ? "系统外观 · 夜间" : "系统外观 · 日间");
                })
                .setNegativeButton("关闭", null)
                .show();
    }

    private void showGlobalAnnotationCenter(String kind, String title) {
        List<ClickNativeStore.GlobalAnnotationRow> rows = store.globalAnnotations(kind, 100);
        libraryVisible = false;
        root.removeAllViews();
        LinearLayout toolbar = compactHeader();
        toolbar.addView(pillButton("书架", v -> renderLibrary("本地书架")));
        TextView heading = smallText(title + " · " + rows.size(), 17, Color.WHITE);
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        toolbar.addView(heading, weightParams());
        root.addView(toolbar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(54)));

        ScrollView scroll = new ScrollView(this);
        LinearLayout list = localResultList();
        if (rows.isEmpty()) {
            list.addView(localResultEmpty("这里还没有" + title.replace("中心", "") + "。"));
        } else {
            java.util.LinkedHashMap<String, java.util.List<ClickNativeStore.GlobalAnnotationRow>> grouped =
                    new java.util.LinkedHashMap<>();
            for (ClickNativeStore.GlobalAnnotationRow row : rows) {
                grouped.computeIfAbsent(row.bookId, ignored -> new java.util.ArrayList<>()).add(row);
            }
            for (java.util.List<ClickNativeStore.GlobalAnnotationRow> bookRows : grouped.values()) {
                ClickNativeStore.GlobalAnnotationRow first = bookRows.get(0);
                TextView bookHeading = smallText(
                        first.bookTitle + " · " + bookRows.size() + " 条",
                        14,
                        Color.rgb(190, 192, 184)
                );
                bookHeading.setTypeface(Typeface.DEFAULT_BOLD);
                bookHeading.setPadding(dp(2), dp(14), 0, dp(8));
                list.addView(bookHeading);
                for (ClickNativeStore.GlobalAnnotationRow row : bookRows) {
                    String chapterLine = row.chapterTitle.isEmpty() ? "未标注章节" : row.chapterTitle;
                    String rawSummary = row.noteText.isEmpty()
                            ? row.sourceText
                            : row.noteText + (row.sourceText.isEmpty() ? "" : " — " + row.sourceText);
                    TextView card = localResultCard(
                            chapterLine + "\n" + ClickNativeStore.searchSnippet(rawSummary, "")
                    );
                    card.setOnClickListener(v -> openBookAtLocator(row.bookId, row.targetLocator));
                    list.addView(card, localResultCardParams());
                }
            }
        }
        scroll.addView(list);
        root.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
    }

    private void showOfflineSearchDialog() {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setHint("书名、作者、笔记或正文");
        input.setTextColor(Color.BLACK);
        new AlertDialog.Builder(this)
                .setTitle("离线全文搜索")
                .setMessage("只搜索手机已缓存的内容，不连接网络。")
                .setView(input)
                .setPositiveButton("搜索", (dialog, which) -> {
                    String query = input.getText().toString().trim();
                    if (query.isEmpty()) {
                        toast("请输入搜索内容");
                        return;
                    }
                    showOfflineSearchResults(query);
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void showVoiceNotesCenter() {
        libraryVisible = false;
        root.removeAllViews();
        LinearLayout toolbar = compactHeader();
        toolbar.addView(pillButton("书架", v -> renderLibrary("本地书架")));
        TextView heading = smallText("语音备注", 17, Color.WHITE);
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        toolbar.addView(heading, weightParams());
        root.addView(toolbar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(54)
        ));

        ScrollView scroll = new ScrollView(this);
        LinearLayout list = localResultList();
        int count = 0;
        for (ClickNativeStore.BookRow book : store.books()) {
            for (ClickNativeStore.AnnotationRow row : store.annotations(book.id, "audio_note")) {
                count += 1;
                String summary = row.noteText.isEmpty() ? row.sourceText : row.noteText;
                TextView card = localResultCard(
                        book.title + "\n" + ClickNativeStore.searchSnippet(summary, "")
                                + "\n点按播放手机私有原音"
                );
                card.setOnClickListener(v -> playLocalVoiceNote(row));
                list.addView(card, localResultCardParams());
            }
        }
        if (count == 0) {
            list.addView(localResultEmpty("手机里还没有语音备注。"));
        }
        scroll.addView(list);
        root.addView(scroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1
        ));
    }

    private void playLocalVoiceNote(ClickNativeStore.AnnotationRow row) {
        if (row.id.equals(playingVoiceNoteId)) {
            releaseVoiceNotePlayer();
            toast("语音已停止");
            return;
        }
        releaseVoiceNotePlayer();
        playingVoiceNoteId = row.id;
        executor.execute(() -> {
            try {
                File file = audioNoteRepository.resolve(new JSONObject(row.metadataJson));
                if (file == null) {
                    throw new IllegalStateException("这条语音没有手机原音");
                }
                runOnUiThread(() -> {
                    if (!row.id.equals(playingVoiceNoteId)) {
                        return;
                    }
                    MediaPlayer player = new MediaPlayer();
                    voiceNotePlayer = player;
                    try {
                        player.setDataSource(file.getAbsolutePath());
                        player.setOnPreparedListener(mediaPlayer -> {
                            mediaPlayer.start();
                            toast("正在播放手机原音");
                        });
                        player.setOnCompletionListener(mediaPlayer -> {
                            releaseVoiceNotePlayer();
                            toast("语音播放完成");
                        });
                        player.setOnErrorListener((mediaPlayer, what, extra) -> {
                            releaseVoiceNotePlayer();
                            toast("语音播放失败");
                            return true;
                        });
                        player.prepareAsync();
                    } catch (Throwable error) {
                        releaseVoiceNotePlayer();
                        toast("语音播放失败：" + compact(error));
                    }
                });
            } catch (Throwable error) {
                runOnUiThread(() -> {
                    releaseVoiceNotePlayer();
                    toast("语音播放失败：" + compact(error));
                });
            }
        });
    }

    private void releaseVoiceNotePlayer() {
        if (voiceNotePlayer != null) {
            try {
                voiceNotePlayer.stop();
            } catch (Throwable ignored) {
                // A player may still be preparing.
            }
            try {
                voiceNotePlayer.release();
            } catch (Throwable ignored) {
                // Best-effort release during navigation or shutdown.
            }
        }
        voiceNotePlayer = null;
        playingVoiceNoteId = "";
    }

    private void showOfflineSearchResults(String query) {
        List<ClickNativeStore.OfflineSearchRow> rows = store.searchOffline(query, 100);
        libraryVisible = false;
        root.removeAllViews();
        LinearLayout toolbar = compactHeader();
        toolbar.addView(pillButton("书架", v -> renderLibrary("本地书架")));
        TextView heading = smallText("离线搜索 · " + query + " · " + rows.size(), 16, Color.WHITE);
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        heading.setSingleLine(true);
        heading.setEllipsize(android.text.TextUtils.TruncateAt.END);
        toolbar.addView(heading, weightParams());
        toolbar.addView(pillButton("再搜", v -> showOfflineSearchDialog()));
        root.addView(toolbar, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(54)));

        ScrollView scroll = new ScrollView(this);
        LinearLayout list = localResultList();
        if (rows.isEmpty()) {
            list.addView(localResultEmpty("手机缓存里没有找到“" + query + "”。"));
        } else {
            java.util.LinkedHashMap<String, java.util.List<ClickNativeStore.OfflineSearchRow>> grouped =
                    new java.util.LinkedHashMap<>();
            for (ClickNativeStore.OfflineSearchRow row : rows) {
                grouped.computeIfAbsent(row.bookId, ignored -> new java.util.ArrayList<>()).add(row);
            }
            for (java.util.List<ClickNativeStore.OfflineSearchRow> bookRows : grouped.values()) {
                ClickNativeStore.OfflineSearchRow first = bookRows.get(0);
                TextView bookHeading = smallText(
                        first.bookTitle + " · " + bookRows.size() + " 条",
                        14,
                        Color.rgb(190, 192, 184)
                );
                bookHeading.setTypeface(Typeface.DEFAULT_BOLD);
                bookHeading.setPadding(dp(2), dp(14), 0, dp(8));
                list.addView(bookHeading);
                for (ClickNativeStore.OfflineSearchRow row : bookRows) {
                    String typeLabel = "book".equals(row.resultType)
                            ? "书籍"
                            : "annotation".equals(row.resultType) ? "批注" : "正文";
                    String chapterLine = row.chapterTitle.isEmpty() ? "打开书籍" : row.chapterTitle;
                    TextView card = localResultCard(
                            typeLabel + " · " + chapterLine + "\n" + row.snippet
                    );
                    card.setOnClickListener(v -> openBookAtLocator(row.bookId, row.targetLocator));
                    list.addView(card, localResultCardParams());
                }
            }
        }
        scroll.addView(list);
        root.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
    }

    private void showSyncIssuesCenter() {
        List<ClickNativeStore.SyncIssueRow> rows = store.syncIssues("", true, 100);
        int activeCount = store.syncIssueCount();
        libraryVisible = false;
        root.removeAllViews();

        LinearLayout toolbar = compactHeader();
        toolbar.addView(pillButton("书架", v -> renderLibrary("本地书架")));
        TextView heading = smallText("同步问题 · " + activeCount, 17, Color.WHITE);
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        toolbar.addView(heading, weightParams());
        root.addView(toolbar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(54)
        ));

        ScrollView scroll = new ScrollView(this);
        LinearLayout list = localResultList();
        if (rows.isEmpty()) {
            list.addView(localResultEmpty("没有同步问题。手机离线内容仍会保留。"));
        } else {
            boolean historyHeadingAdded = false;
            for (ClickNativeStore.SyncIssueRow row : rows) {
                if (!row.isActive() && !historyHeadingAdded) {
                    TextView history = smallText(
                            "处理记录与仅本机内容",
                            13,
                            Color.rgb(143, 145, 150)
                    );
                    history.setPadding(dp(2), dp(16), 0, dp(10));
                    list.addView(history);
                    historyHeadingAdded = true;
                }
                list.addView(syncIssueCard(row), localResultCardParams());
            }
        }
        scroll.addView(list);
        root.addView(scroll, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1
        ));
    }

    private View syncIssueCard(ClickNativeStore.SyncIssueRow row) {
        LinearLayout card = new LinearLayout(this);
        card.setOrientation(LinearLayout.VERTICAL);
        card.setPadding(dp(14), dp(12), dp(14), dp(12));
        card.setBackground(rounded(
                row.isActive() ? Color.rgb(31, 27, 25) : Color.rgb(24, 25, 27),
                dp(10),
                row.isActive() ? Color.rgb(132, 82, 62) : Color.rgb(48, 50, 54),
                1
        ));

        String book = row.bookTitle.isEmpty()
                ? (row.bookId.isEmpty() ? "未知书籍" : row.bookId)
                : row.bookTitle;
        TextView title = text(
                issueStatusLabel(row.status) + " · " + book + " · "
                        + issueOperationLabel(row.operationType),
                15,
                Color.rgb(242, 242, 239)
        );
        title.setTypeface(Typeface.DEFAULT_BOLD);
        card.addView(title, matchWrap());

        TextView comparison = smallText(
                "Mac：" + issueMacSummary(row)
                        + "\n手机：" + issuePhoneSummary(row)
                        + "\n原因：" + issueReason(row),
                14,
                row.isActive() ? Color.rgb(220, 205, 193) : Color.rgb(164, 166, 170)
        );
        comparison.setPadding(0, dp(8), 0, row.isActive() ? dp(10) : 0);
        comparison.setMaxLines(9);
        comparison.setEllipsize(android.text.TextUtils.TruncateAt.END);
        card.addView(comparison, matchWrap());

        if (!row.isActive()) {
            return card;
        }

        LinearLayout actions = new LinearLayout(this);
        actions.setOrientation(LinearLayout.VERTICAL);
        actions.addView(issueActionButton(
                row.isConflict() ? "采用 Mac 版本" : "采用 Mac / 放弃手机操作",
                v -> confirmUseServer(row)
        ), matchFixedHeight(46));
        if (row.isConflict()) {
            String keepLabel = issueServerDeleted(row)
                    && "annotation_updated".equals(row.operationType)
                    ? "恢复为新批注"
                    : "保留手机版本并重新提交";
            LinearLayout.LayoutParams keepParams = matchFixedHeight(46);
            keepParams.setMargins(0, dp(6), 0, 0);
            actions.addView(issueActionButton(
                    keepLabel,
                    v -> confirmKeepDevice(row, keepLabel)
            ), keepParams);
        } else if (row.supportsLocalOnly()) {
            LinearLayout.LayoutParams localParams = matchFixedHeight(46);
            localParams.setMargins(0, dp(6), 0, 0);
            actions.addView(issueActionButton(
                    "仅保留在手机",
                    v -> confirmKeepLocalOnly(row)
            ), localParams);
        }
        card.addView(actions, matchWrap());
        return card;
    }

    private Button issueActionButton(String label, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setTextSize(14);
        button.setGravity(Gravity.CENTER);
        button.setOnClickListener(listener);
        return button;
    }

    private void confirmUseServer(ClickNativeStore.SyncIssueRow row) {
        String title = row.isConflict() ? "采用 Mac 版本？" : "放弃手机这次操作？";
        String message = row.isConflict()
                ? "会放弃手机这次修改，并恢复为上面显示的 Mac 版本。不会删除 Mac 上的内容。"
                : "手机这次未能同步的修改将被放弃。若属于 Mac 元数据，系统会在联网后重新取回 Mac 版本。";
        new AlertDialog.Builder(this)
                .setTitle(title)
                .setMessage(message)
                .setPositiveButton("确认采用 Mac", (dialog, which) -> {
                    try {
                        store.resolveUseServer(row.operationId);
                        ClickSyncScheduler.enqueueMetadata(this);
                        showSyncIssuesCenter();
                        toast("已采用 Mac 版本");
                    } catch (Throwable error) {
                        toast("没有改动任何内容：" + compact(error));
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void confirmKeepDevice(ClickNativeStore.SyncIssueRow row, String actionLabel) {
        long serverVersion = issueReceipt(row).optLong("server_version", 0L);
        String message = "会以 Mac 当前版本"
                + (serverVersion > 0L ? " v" + serverVersion : "")
                + " 为依据，用新的操作编号重新提交。若 Mac 又发生变化，会再次停下等待选择，不会自动覆盖。";
        if ("恢复为新批注".equals(actionLabel)) {
            message = "Mac 上原批注已经删除。手机内容会以一条新的批注恢复，不会悄悄复活原记录。"
                    + "若再次冲突，仍会停下等待选择。";
        }
        new AlertDialog.Builder(this)
                .setTitle(actionLabel + "？")
                .setMessage(message)
                .setPositiveButton("确认", (dialog, which) -> {
                    try {
                        store.resolveKeepDevice(row.operationId, operationId());
                        ClickSyncScheduler.enqueueMetadata(this);
                        showSyncIssuesCenter();
                        toast("手机版本已排队；不会静默覆盖 Mac");
                    } catch (Throwable error) {
                        toast("没有改动任何内容：" + compact(error));
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void confirmKeepLocalOnly(ClickNativeStore.SyncIssueRow row) {
        new AlertDialog.Builder(this)
                .setTitle("仅保留在手机？")
                .setMessage(
                        "这项内容以后只保留在手机，不会自动重试；Mac 的变化也不会静默覆盖它。"
                                + "记录会继续留在“同步问题”历史中，可离线查看。"
                )
                .setPositiveButton("仅保留在手机", (dialog, which) -> {
                    try {
                        store.keepPermanentLocalOnly(row.operationId);
                        ClickSyncScheduler.enqueueMetadata(this);
                        showSyncIssuesCenter();
                        toast("已标记为仅本机内容");
                    } catch (Throwable error) {
                        toast("没有改动任何内容：" + compact(error));
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private JSONObject issueReceipt(ClickNativeStore.SyncIssueRow row) {
        try {
            return row.receiptJson.isEmpty()
                    ? new JSONObject()
                    : new JSONObject(row.receiptJson);
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private JSONObject issuePayload(ClickNativeStore.SyncIssueRow row) {
        try {
            return row.payloadJson.isEmpty()
                    ? new JSONObject()
                    : new JSONObject(row.payloadJson);
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private boolean issueServerDeleted(ClickNativeStore.SyncIssueRow row) {
        JSONObject receipt = issueReceipt(row);
        return receipt.optBoolean("server_deleted")
                || receipt.optJSONObject("server_record") == null;
    }

    private String issueMacSummary(ClickNativeStore.SyncIssueRow row) {
        JSONObject receipt = issueReceipt(row);
        if (!row.isConflict()) {
            return "未接受手机这次操作，Mac 当前内容保持不变";
        }
        JSONObject server = receipt.optJSONObject("server_record");
        if (receipt.optBoolean("server_deleted") || server == null) {
            return "这条内容已删除或不存在";
        }
        return issueText(
                server.optString("note_text"),
                server.optString("source_text"),
                "Mac 上的批注仍存在"
        );
    }

    private String issuePhoneSummary(ClickNativeStore.SyncIssueRow row) {
        JSONObject payload = issuePayload(row);
        if ("annotation_deleted".equals(row.operationType)) {
            return "要求删除这条批注";
        }
        if ("reading_position_updated".equals(row.operationType)) {
            return "阅读到 " + issueText(
                    payload.optString("chapter_locator"),
                    payload.optString("page_index"),
                    "新的阅读位置"
            );
        }
        return issueText(
                payload.optString("note_text"),
                payload.optString("source_text"),
                "手机本地修改仍保留"
        );
    }

    private String issueReason(ClickNativeStore.SyncIssueRow row) {
        String raw = row.lastError.isEmpty()
                ? issueReceipt(row).optString("error")
                : row.lastError;
        if (raw.contains("changed on another device")) {
            return "Mac 上的内容已经被其他设备修改";
        }
        if (raw.contains("deleted or does not exist")) {
            return "Mac 上这条内容已经删除或不存在";
        }
        if (raw.contains("base_server_version is required")) {
            return "手机缺少修改所依据的 Mac 版本";
        }
        if (raw.contains("book does not exist")) {
            return "这本书已不在 Mac 书库";
        }
        return issueText(raw, "", row.isConflict() ? "Mac 与手机同时发生了变化" : "服务端拒绝了这次操作");
    }

    private String issueOperationLabel(String operationType) {
        if ("annotation_updated".equals(operationType)) return "修改批注";
        if ("annotation_deleted".equals(operationType)) return "删除批注";
        if ("annotation_created".equals(operationType)
                || "note_created".equals(operationType)
                || "red_highlight_created".equals(operationType)) return "新批注";
        if ("audio_note_created".equals(operationType)) return "语音备注";
        if ("reading_position_updated".equals(operationType)) return "阅读位置";
        if ("book_organization_updated".equals(operationType)) return "书籍整理";
        return "手机操作";
    }

    private String issueStatusLabel(String status) {
        if ("conflict".equals(status)) return "需要选择";
        if ("permanent_failed".equals(status)) return "无法同步（不会自动重试）";
        if ("resolved_server".equals(status)) return "已采用 Mac";
        if ("superseded".equals(status)) return "已由新操作替代";
        if ("local_only".equals(status)) return "仅保留在手机";
        return "处理记录";
    }

    private String issueText(String primary, String secondary, String fallback) {
        String value = safe(primary).trim();
        if (value.isEmpty()) value = safe(secondary).trim();
        if (value.isEmpty()) value = fallback;
        value = value.replaceAll("\\s+", " ").trim();
        return value.length() > 120 ? value.substring(0, 120) + "…" : value;
    }

    private LinearLayout localResultList() {
        LinearLayout list = new LinearLayout(this);
        list.setOrientation(LinearLayout.VERTICAL);
        list.setPadding(dp(14), dp(12), dp(14), dp(36));
        return list;
    }

    private TextView localResultCard(String value) {
        TextView card = text(value, 15, Color.rgb(232, 233, 235));
        card.setMaxLines(5);
        card.setEllipsize(android.text.TextUtils.TruncateAt.END);
        card.setGravity(Gravity.CENTER_VERTICAL);
        card.setPadding(dp(14), dp(10), dp(14), dp(10));
        card.setBackground(rounded(Color.rgb(25, 27, 30), dp(10), Color.rgb(46, 49, 54), 1));
        return card;
    }

    private TextView localResultEmpty(String value) {
        TextView empty = text(value, 16, Color.rgb(167, 168, 172));
        empty.setGravity(Gravity.CENTER);
        empty.setPadding(dp(18), dp(64), dp(18), 0);
        return empty;
    }

    private LinearLayout.LayoutParams localResultCardParams() {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
        params.setMargins(0, 0, 0, dp(9));
        return params;
    }

    private void showLibraryFilterDialog() {
        final String[] items = {
                ("recent".equals(librarySort) ? "✓ " : "") + "排序：最近同步",
                ("title".equals(librarySort) ? "✓ " : "") + "排序：书名",
                ("author".equals(librarySort) ? "✓ " : "") + "排序：作者",
                ("all".equals(libraryReadingState) ? "✓ " : "") + "状态：全部",
                ("unread".equals(libraryReadingState) ? "✓ " : "") + "状态：未读",
                ("reading".equals(libraryReadingState) ? "✓ " : "") + "状态：正在阅读"
        };
        new AlertDialog.Builder(this)
                .setTitle("书库筛选")
                .setItems(items, (dialog, which) -> {
                    if (which == 0) librarySort = "recent";
                    if (which == 1) librarySort = "title";
                    if (which == 2) librarySort = "author";
                    if (which == 3) libraryReadingState = "all";
                    if (which == 4) libraryReadingState = "unread";
                    if (which == 5) libraryReadingState = "reading";
                    renderLibrary("筛选已应用 · " + visibleBooks().size() + " 本");
                })
                .setNegativeButton("关闭", null)
                .show();
    }

    private void showLibraryMoreDialog() {
        final String[] items = {
                "导入 EPUB / PDF",
                "筛选与排序",
                "同步全部 · " + pendingSyncItemCount(store.pendingCount(), store.pendingBookImportCount()) + " 项待处理",
                "清除搜索和筛选",
                "阅读设置",
                "下载设置"
        };
        new AlertDialog.Builder(this)
                .setTitle("更多")
                .setItems(items, (dialog, which) -> {
                    if (which == 0) {
                        openBookImport();
                    } else if (which == 1) {
                        showLibraryFilterDialog();
                    } else if (which == 2) {
                        syncFullThenRender();
                    } else if (which == 3) {
                        libraryQuery = "";
                        librarySort = "recent";
                        libraryReadingState = "all";
                        libraryMode = "all";
                        libraryCategory = "";
                        libraryAuthor = "";
                        renderLibrary("已清除书库筛选");
                    } else if (which == 4) {
                        showVisualSettingsPanel();
                    } else {
                        showDownloadSettingsPanel();
                    }
                })
                .show();
    }

    private void showBookMoreDialog(ClickNativeStore.BookRow book) {
        final String analysisLabel = analysisActionLabel(book.analysisState);
        final String favoriteLabel = book.favorite ? "取消收藏" : "收藏";
        final String categoryLabel = book.customCategory.isEmpty()
                ? "移动到文件夹"
                : "文件夹：" + book.customCategory;
        final String offlineState = bookOfflineState(book);
        final List<String> itemList = new ArrayList<>();
        itemList.add("继续阅读");
        itemList.add("目录");
        itemList.add(favoriteLabel);
        itemList.add(categoryLabel);
        itemList.add(analysisLabel);
        itemList.add("同步本书");
        if (ClickNativeStore.IMPORT_TERMINAL.equals(book.importState)) {
            itemList.add("重新导入原书");
        } else if ("同步失败".equals(offlineState)) {
            itemList.add("重试下载");
        } else if ("需下载".equals(offlineState)) {
            itemList.add("下载到本机");
        }
        if (shareableOriginalCandidate(book) != null) {
            itemList.add("分享原书");
        }
        itemList.add("分享笔记（纯文本）");
        itemList.add("书籍信息");
        if (localBookFile(book) != null) {
            itemList.add("删除本地副本");
        }
        final String[] items = itemList.toArray(new String[0]);
        new AlertDialog.Builder(this)
                .setTitle(book.title)
                .setItems(items, (dialog, which) -> {
                    String action = items[which];
                    if ("继续阅读".equals(action)) {
                        openBook(book.id, true);
                    } else if ("目录".equals(action)) {
                        openBook(book.id, false);
                    } else if (favoriteLabel.equals(action)) {
                        toggleFavorite(book);
                    } else if (categoryLabel.equals(action)) {
                        showBookCategoryDialog(book);
                    } else if (analysisLabel.equals(action)) {
                        handleLivingBookAnalysisAction(book);
                    } else if ("同步本书".equals(action)) {
                        syncChangesThenRender();
                    } else if ("重新导入原书".equals(action)) {
                        openBookImport();
                    } else if ("重试下载".equals(action)) {
                        retryBookCache(book);
                    } else if ("下载到本机".equals(action)) {
                        downloadBookToDevice(book);
                    } else if ("分享原书".equals(action)) {
                        shareBookOriginal(book);
                    } else if ("分享笔记（纯文本）".equals(action)) {
                        shareBookNotes(book);
                    } else if ("书籍信息".equals(action)) {
                        toast(book.title + "\n" + (book.author.isEmpty() ? "未知作者" : book.author) + "\n" + bookStateLabel(book));
                    } else if ("删除本地副本".equals(action)) {
                        confirmDeleteBookLocalCopy(book);
                    }
                })
                .show();
    }

    private void retryBookCache(ClickNativeStore.BookRow book) {
        try {
            ClickNativeStore.BookRetryResult result = store.retryTerminalBookWork(book.id);
            if (!result.hasWork()) {
                downloadBookToDevice(book);
                return;
            }
            if (result.assetRowsReset > 0) {
                ClickSyncScheduler.enqueueAssets(this);
            }
            if (result.metadataRowsReset > 0) {
                ClickSyncScheduler.replaceMetadataRepairFromStore(this);
            }
            renderLibrary("已重新排队《" + book.title + "》的缓存");
        } catch (Throwable error) {
            toast("缓存重试失败：" + compact(error));
        }
    }

    private void downloadBookToDevice(ClickNativeStore.BookRow book) {
        if (book.sourceByteSize <= 0L) {
            toast("缺少原书预计大小，请先同步一次书架");
            return;
        }
        if (activeNetworkIsMetered()) {
            boolean globalMetered = ClickSyncConfig.allowMeteredAssets(this);
            String message = "《"
                    + book.title
                    + "》预计 "
                    + formatEstimatedBytes(book.sourceByteSize)
                    + "。"
                    + (globalMetered
                    ? "\n当前“所有大文件允许蜂窝网络”已开启。"
                    : "\n后台大文件仍默认等待 Wi‑Fi。");
            new AlertDialog.Builder(this)
                    .setTitle("使用计费网络下载？")
                    .setMessage(message)
                    .setPositiveButton(
                            "仅这一本使用蜂窝",
                            (dialog, which) -> queueBookSourceDownload(book, true)
                    )
                    .setNeutralButton(
                            "等 Wi‑Fi",
                            (dialog, which) -> queueBookSourceDownload(book, false)
                    )
                    .setNegativeButton("取消", null)
                    .show();
            return;
        }
        queueBookSourceDownload(book, false);
    }

    private void queueBookSourceDownload(
            ClickNativeStore.BookRow book,
            boolean allowMetered
    ) {
        try {
            if (!store.requestBookSourceDownload(book.id)) {
                toast("缺少可信的原书下载合同，请先同步一次书架");
                return;
            }
            ClickSyncScheduler.enqueueExplicitBookSource(this, book.id, allowMetered);
            renderLibrary(
                    "已排队下载《"
                            + book.title
                            + "》；"
                            + (allowMetered ? "仅这一本可使用蜂窝网络" : "等待 Wi‑Fi")
            );
        } catch (Throwable error) {
            toast("下载排队失败：" + compact(error));
        }
    }

    private boolean activeNetworkIsMetered() {
        ConnectivityManager manager =
                (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        return manager != null
                && manager.getActiveNetwork() != null
                && manager.isActiveNetworkMetered();
    }

    static String formatEstimatedBytes(long byteSize) {
        if (byteSize <= 0L) {
            return "大小未知";
        }
        double mebibytes = byteSize / (1024d * 1024d);
        return String.format(java.util.Locale.ROOT, "%.1f MB", mebibytes);
    }

    private String analysisActionLabel(String state) {
        if ("queued".equals(state) || "running".equals(state)) return "分析状态";
        if ("needs_review".equals(state) || "complete".equals(state)) return "重新分析";
        return "分析";
    }

    private String bookStateLabel(ClickNativeStore.BookRow book) {
        String profile = "IMAGE_COMIC".equals(book.readingProfile) ? "漫画" : "文字";
        String type = "pdf".equals(book.sourceKind) ? "PDF" : "EPUB";
        String analysis;
        switch (book.analysisState) {
            case "queued": analysis = "分析已排队"; break;
            case "running": analysis = "分析中"; break;
            case "needs_review": analysis = "分析待审阅"; break;
            case "complete": analysis = "分析完成"; break;
            case "failed": analysis = "分析失败"; break;
            default: analysis = "未分析"; break;
        }
        String assetIssue = book.assetIssueSummary;
        return type
                + " · "
                + bookOfflineState(book)
                + " · "
                + profile
                + " · "
                + analysis
                + (book.correctionQueueRequired ? " · 待纠错" : "")
                + (assetIssue.isEmpty() ? "" : " · " + assetIssue);
    }

    private void handleLivingBookAnalysisAction(ClickNativeStore.BookRow book) {
        if ("queued".equals(book.analysisState) || "running".equals(book.analysisState)) {
            toast(bookStateLabel(book));
            syncChangesThenRender();
            return;
        }
        if ("needs_review".equals(book.analysisState) || "complete".equals(book.analysisState)) {
            new AlertDialog.Builder(this)
                    .setTitle("重新分析这本书？")
                    .setMessage("现有人工审阅文件不会被覆盖；新结果会写入 .new 草稿。")
                    .setPositiveButton("重新分析", (dialog, which) -> requestLivingBookAnalysis(book, true))
                    .setNegativeButton("取消", null)
                    .show();
            return;
        }
        requestLivingBookAnalysis(book, false);
    }

    private void requestLivingBookAnalysis(ClickNativeStore.BookRow book, boolean force) {
        try {
            JSONObject payload = new JSONObject()
                    .put("book_id", book.id)
                    .put("force", force);
            String operationId = "android-living-book-analysis-" + book.id + (force ? "-reanalyze" : "-initial");
            store.setBookAnalysisStateAndEnqueue(
                    operationId,
                    book.id,
                    "queued",
                    payload
            );
            ClickSyncScheduler.enqueueMetadata(this);
            renderLibrary("已排队分析《" + book.title + "》；联网后自动同步");
        } catch (Throwable error) {
            toast("分析请求保存失败：" + compact(error));
        }
    }

    private void showLibrarySearchDialog() {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setText(libraryQuery);
        input.setHint("输入书名、作者或文件夹");
        new AlertDialog.Builder(this)
                .setTitle("搜索本地书库")
                .setView(input)
                .setPositiveButton("搜索", (dialog, which) -> {
                    libraryQuery = input.getText().toString().trim();
                    renderLibrary(libraryQuery.isEmpty() ? "已显示全部书籍" : "搜索到 " + visibleBooks().size() + " 本");
                })
                .setNeutralButton("清除", (dialog, which) -> {
                    libraryQuery = "";
                    renderLibrary("已清除搜索");
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private List<ClickNativeStore.BookRow> visibleBooks() {
        List<ClickNativeStore.BookRow> result = new ArrayList<>();
        String needle = libraryQuery.toLowerCase(java.util.Locale.ROOT);
        for (ClickNativeStore.BookRow book : store.books()) {
            boolean matchesQuery = needle.isEmpty()
                    || book.title.toLowerCase(java.util.Locale.ROOT).contains(needle)
                    || book.author.toLowerCase(java.util.Locale.ROOT).contains(needle)
                    || book.customCategory.toLowerCase(java.util.Locale.ROOT).contains(needle);
            boolean matchesState = "all".equals(libraryReadingState)
                    || ("reading".equals(libraryReadingState) && book.hasPosition)
                    || ("unread".equals(libraryReadingState) && !book.hasPosition);
            boolean matchesMode = !"favorites".equals(libraryMode) || book.favorite;
            if ("recent".equals(libraryMode)) {
                matchesMode = book.hasPosition || !book.lastOpenedAt.isEmpty();
            }
            if ("category".equals(libraryMode)) {
                matchesMode = !libraryCategory.isEmpty() && libraryCategory.equals(book.customCategory);
            }
            if ("author".equals(libraryMode)) {
                matchesMode = !libraryAuthor.isEmpty() && libraryAuthor.equals(displayAuthor(book));
            }
            if (matchesQuery && matchesState && matchesMode) result.add(book);
        }
        if ("title".equals(librarySort)) {
            result.sort((left, right) -> left.title.compareToIgnoreCase(right.title));
        } else if ("author".equals(librarySort)) {
            result.sort((left, right) -> left.author.compareToIgnoreCase(right.author));
        }
        return result;
    }

    private String emptyLibraryMessage() {
        if ("favorites".equals(libraryMode)) return "还没有收藏。长按一本书可加入收藏。";
        if ("recent".equals(libraryMode)) return "还没有最近阅读记录。";
        if ("category".equals(libraryMode)) return "“" + libraryCategory + "”文件夹里还没有书。";
        if ("author".equals(libraryMode)) return "没有找到“" + libraryAuthor + "”的书。";
        if (!libraryQuery.isEmpty()) return "没有找到匹配的书。";
        return "还没有离线书籍。返回连接页设置 Mac 后完成首次同步。";
    }

    private Button libraryChip(String label, boolean selected, View.OnClickListener listener) {
        Button button = smallButton(label, listener);
        ClickUi.stylePill(button, selected, visualDarkMode);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.WRAP_CONTENT, dp(40));
        params.setMargins(0, 0, dp(8), 0);
        button.setLayoutParams(params);
        return button;
    }

    private void toggleFavorite(ClickNativeStore.BookRow book) {
        persistBookOrganization(book, !book.favorite, book.customCategory, "收藏已更新");
    }

    private void showBookCategoryDialog(ClickNativeStore.BookRow book) {
        java.util.TreeSet<String> folderSet = new java.util.TreeSet<>();
        for (ClickNativeStore.BookRow row : store.books()) {
            if (!row.customCategory.isEmpty()) folderSet.add(row.customCategory);
        }
        List<String> folders = new ArrayList<>(folderSet);
        int removeOffset = 1 + folders.size();
        String[] items = new String[removeOffset + (book.customCategory.isEmpty() ? 0 : 1)];
        items[0] = "＋ 新建文件夹";
        for (int index = 0; index < folders.size(); index++) {
            String folder = folders.get(index);
            items[index + 1] = (folder.equals(book.customCategory) ? "✓ " : "") + folder;
        }
        if (!book.customCategory.isEmpty()) items[removeOffset] = "移出当前文件夹";
        new AlertDialog.Builder(this)
                .setTitle("移动到文件夹")
                .setItems(items, (dialog, which) -> {
                    if (which == 0) {
                        showCreateFolderForBookDialog(book);
                    } else if (which <= folders.size()) {
                        persistBookOrganization(book, book.favorite, folders.get(which - 1), "已移动到文件夹");
                    } else {
                        persistBookOrganization(book, book.favorite, "", "已移出文件夹");
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void showCreateFolderForBookDialog(ClickNativeStore.BookRow book) {
        EditText input = new EditText(this);
        input.setSingleLine(true);
        input.setHint("例如：经济学");
        input.setTextColor(Color.rgb(244, 244, 242));
        input.setHintTextColor(Color.rgb(125, 127, 132));
        new AlertDialog.Builder(this)
                .setTitle("新建文件夹")
                .setView(input)
                .setPositiveButton("创建并放入", (dialog, which) -> {
                    String folder = normalizedFolderName(input.getText().toString());
                    if (folder.isEmpty()) {
                        toast("请输入 1–40 个字符的文件夹名称");
                    } else {
                        persistBookOrganization(book, book.favorite, folder, "已移动到文件夹");
                    }
                })
                .setNegativeButton("取消", null)
                .show();
    }

    private void persistBookOrganization(ClickNativeStore.BookRow book, boolean favorite, String category, String message) {
        try {
            JSONArray tags = new JSONArray(book.tagsJson);
            store.setBookOrganizationAndEnqueue(
                    operationId(),
                    book.id,
                    favorite,
                    category,
                    tags
            );
            ClickSyncScheduler.enqueueMetadata(this);
            renderLibrary(message);
        } catch (Throwable error) {
            toast("书籍整理保存失败：" + compact(error));
        }
    }

    private File shareableOriginalCandidate(ClickNativeStore.BookRow book) {
        if (book == null
                || (!"epub".equals(book.sourceKind) && !"pdf".equals(book.sourceKind))
                || !book.sourceHash.matches("[0-9a-f]{64}")
                || book.sourceByteSize <= 0L) {
            return null;
        }
        File source = appPrivateReadableFile(book.sourceLocalPath);
        if (source == null) {
            return null;
        }
        try {
            File root = getFilesDir().getCanonicalFile();
            File importedRoot = new File(root, BookImportIngress.SOURCE_ROOT_NAME).getCanonicalFile();
            File downloadedRoot = new File(root, "click-source-library").getCanonicalFile();
            String path = source.getCanonicalPath();
            boolean inImportedRoot = path.startsWith(importedRoot.getPath() + File.separator);
            boolean inDownloadedRoot = path.startsWith(downloadedRoot.getPath() + File.separator);
            return inImportedRoot || inDownloadedRoot ? source : null;
        } catch (Throwable ignored) {
            return null;
        }
    }

    private void shareBookOriginal(ClickNativeStore.BookRow book) {
        File candidate = shareableOriginalCandidate(book);
        if (candidate == null) {
            toast("这份原书不是可分享的 Click 私有已校验副本");
            return;
        }
        executor.execute(() -> {
            try {
                VerifiedAssetFile.verify(
                        candidate,
                        book.sourceHash,
                        book.sourceByteSize
                );
                if (!book.sourceKind.equals(BookImportIngress.detectSourceKind(candidate))) {
                    throw new IllegalStateException("原书格式与书库记录不一致");
                }
                Uri uri = FileProvider.getUriForFile(
                        this,
                        getPackageName() + ".bookfiles",
                        candidate
                );
                Intent share = new Intent(Intent.ACTION_SEND);
                share.setType(
                        "pdf".equals(book.sourceKind)
                                ? "application/pdf"
                                : "application/epub+zip"
                );
                share.putExtra(Intent.EXTRA_STREAM, uri);
                share.setClipData(ClipData.newRawUri("Click 原书", uri));
                share.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
                runOnUiThread(() -> {
                    try {
                        startActivity(Intent.createChooser(share, "分享原书"));
                    } catch (Throwable error) {
                        toast("没有可接收这份原书的应用");
                    }
                });
            } catch (Throwable error) {
                runOnUiThread(() -> {
                    if (!isFinishing() && !isDestroyed()) {
                        Toast.makeText(
                                this,
                                "原书重新校验失败，已停止分享",
                                Toast.LENGTH_LONG
                        ).show();
                    }
                });
            }
        });
    }

    private void shareBookNotes(ClickNativeStore.BookRow book) {
        List<ClickNativeStore.AnnotationRow> notes = store.annotations(book.id, "note");
        if (notes.isEmpty()) {
            toast("这本书还没有文字笔记");
            return;
        }
        StringBuilder text = new StringBuilder();
        text.append("《").append(book.title).append("》笔记");
        for (ClickNativeStore.AnnotationRow note : notes) {
            text.append("\n\n");
            if (!note.noteText.isEmpty()) {
                text.append(note.noteText);
            }
            if (!note.sourceText.isEmpty()) {
                if (!note.noteText.isEmpty()) {
                    text.append("\n— ");
                }
                text.append(note.sourceText);
            }
        }
        Intent share = new Intent(Intent.ACTION_SEND);
        share.setType("text/plain");
        share.putExtra(Intent.EXTRA_TEXT, text.toString());
        try {
            startActivity(Intent.createChooser(share, "分享笔记"));
        } catch (Throwable error) {
            toast("没有可接收文本的应用");
        }
    }

    private void confirmDeleteBookLocalCopy(ClickNativeStore.BookRow book) {
        if (!"remote".equals(book.importState) && !"imported".equals(book.importState)) {
            new AlertDialog.Builder(this)
                    .setTitle("不能删除这份原书")
                    .setMessage(
                            "它还没有安全归入 Mac 主库，手机目前是唯一可靠副本。"
                                    + "请先恢复连接完成归档，再删除本地副本。"
                    )
                    .setPositiveButton("知道了", null)
                    .show();
            return;
        }
        new AlertDialog.Builder(this)
                .setTitle("删除本地副本？")
                .setMessage(
                        "只删除手机里《" + book.title + "》的原书和阅读缓存，"
                                + "不会删除 Mac 主库、笔记或阅读进度。以后需要时可手动重新下载。"
                )
                .setPositiveButton("删除本地副本", (dialog, which) -> removeBookLocalCopy(book))
                .setNegativeButton("取消", null)
                .show();
    }

    private void removeBookLocalCopy(ClickNativeStore.BookRow book) {
        executor.execute(() -> {
            try {
                ClickNativeStore.LocalCopyRemovalResult result =
                        store.removeBookLocalCopy(book.id);
                String message = result.cleanupPendingCount > 0
                        ? "本地副本已从书架移除；仍有 "
                                + result.cleanupPendingCount
                                + " 个文件留在清理队列，下次打开书架会继续处理"
                        : "本地副本已删除；Mac 主库、笔记和进度未改变";
                renderLibrary(message);
            } catch (Throwable error) {
                toast(compact(error));
            }
        });
    }

    private void recoverLocalFileCleanupAsync() {
        executor.execute(() -> {
            try {
                store.drainLocalFileCleanupQueue();
            } catch (Throwable ignored) {
                // The durable queue remains intact for the next explicit app start.
            }
        });
    }

    private void showReaderTocDrawer() {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dp(12), 0, dp(12), dp(8));
        panel.setBackgroundColor(Color.rgb(226, 231, 248));
        LinearLayout tabs = moonToolbar();
        tabs.addView(toolbarButton("‹", v -> closeParentPopup(v)));
        TextView toc = text("目录    书签    批注", 21, Color.WHITE);
        toc.setTypeface(Typeface.DEFAULT_BOLD);
        tabs.addView(toc, weightParams());
        panel.addView(tabs, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(58)));

        ScrollView scroll = new ScrollView(this);
        LinearLayout list = new LinearLayout(this);
        list.setOrientation(LinearLayout.VERTICAL);
        for (ClickNativeStore.ChapterRow chapter : currentChapters) {
            TextView row = drawerRow((chapter.index == currentChapterIndex ? "● " : "  ") + chapter.title);
            row.setTextColor(chapter.index == currentChapterIndex ? Color.rgb(45, 71, 120) : Color.rgb(34, 36, 43));
            row.setOnClickListener(v -> {
                closeParentPopup(v);
                openChapter(chapter.index);
            });
            list.addView(row, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(56)));
        }
        scroll.addView(list);
        panel.addView(scroll, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, 0, 1));
        TextView footer = smallText("第 " + (currentChapterIndex + 1) + " 篇 / " + currentChapters.size(), 15, Color.rgb(64, 68, 80));
        footer.setGravity(Gravity.CENTER_VERTICAL);
        panel.addView(footer, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(44)));

        PopupWindow popup = new PopupWindow(panel, Math.round(getResources().getDisplayMetrics().widthPixels * 0.84f), ViewGroup.LayoutParams.MATCH_PARENT, true);
        popup.setOutsideTouchable(true);
        popup.showAtLocation(root, Gravity.START | Gravity.TOP, 0, 0);
        panel.setTag(popup);
    }

    private void showVisualSettingsPanel() {
        LinearLayout panel = settingsPanel("阅读排版");
        TextView preview = text("苦读几年之后，终于得到了博士学位，有些人会变成很富有，但他们仍旧是干渴的。", visualFontSize, visualDarkMode ? Color.WHITE : Color.rgb(24, 24, 24));
        preview.setPadding(dp(12), dp(10), dp(12), dp(10));
        preview.setBackground(rounded(visualDarkMode ? Color.BLACK : Color.rgb(246, 241, 228), dp(12), Color.rgb(198, 204, 220), 1));
        panel.addView(preview, matchWrap());
        panel.addView(settingSeekRow("文字大小", 16, 34, visualFontSize, value -> {
            visualFontSize = value;
            preview.setTextSize(value);
            applyReaderVisualSettings();
        }));
        panel.addView(settingSeekRow("行间距", 110, 180, visualLineHeightPercent, value -> {
            visualLineHeightPercent = value;
            applyReaderVisualSettings();
        }));
        panel.addView(settingSeekRow("页面边距", 0, 34, visualMargin, value -> {
            visualMargin = value;
            applyReaderVisualSettings();
        }));
        panel.addView(settingsButton("恢复紧凑默认", v -> {
            readerPreferences = preferencesRepository.resetLayout();
            visualFontSize = readerPreferences.getFontSizeSp();
            visualLineHeightPercent = readerPreferences.getLineHeightPercent();
            visualMargin = readerPreferences.getSideMarginDp();
            visualDarkMode = readerPreferences.getTheme() == ReaderTheme.NIGHT;
            applyReaderVisualSettings();
            toast("已恢复紧凑阅读设置");
        }));
        showBottomSheetLike(panel);
    }

    @SuppressWarnings("deprecation")
    private void showDownloadSettingsPanel() {
        LinearLayout panel = settingsPanel("下载网络");
        Switch allowCellular = new Switch(this);
        allowCellular.setText("允许所有大文件使用蜂窝网络");
        allowCellular.setTextColor(Color.rgb(232, 233, 235));
        allowCellular.setTextSize(17);
        allowCellular.setChecked(ClickSyncConfig.allowMeteredAssets(this));
        allowCellular.setPadding(0, dp(8), 0, dp(8));
        allowCellular.setOnClickListener(view -> {
            boolean requested = allowCellular.isChecked();
            if (!ClickSyncScheduler.updateMeteredAssetPolicy(this, requested)) {
                allowCellular.setChecked(!requested);
                toast("下载网络设置保存失败");
                return;
            }
            toast(requested ? "所有大文件可使用蜂窝网络" : "后台大文件只在 Wi‑Fi 下载");
        });
        panel.addView(
                allowCellular,
                new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(60))
        );
        TextView explanation = smallText(
                "默认关闭。关闭时后台大文件等待 Wi‑Fi；你仍可在单本下载时仅授权那一本。",
                14,
                Color.rgb(167, 168, 172)
        );
        explanation.setPadding(0, dp(8), 0, dp(8));
        panel.addView(explanation, matchWrap());
        showBottomSheetLike(panel);
    }

    private void showControlSettingsPanel() {
        LinearLayout panel = settingsPanel("控制选项");
        for (String row : new String[]{
                "点击左侧          上一页",
                "点击右侧          下一页",
                "点击中间          打开工具栏",
                "左滑 / 右滑       翻页",
                "滑动节流          一次完整手势只翻一页",
                "返回键            返回上一级，不直接退出 App"
        }) {
            panel.addView(filterRow(row), new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48)));
        }
        showBottomSheetLike(panel);
    }

    private void showReaderMoreDialog() {
        final String[] items = {
                "控制选项",
                "问 AI · 基于本书和笔记",
                "同步状态",
                "书籍信息",
                "返回书架"
        };
        new AlertDialog.Builder(this)
                .setTitle("更多")
                .setItems(items, (dialog, which) -> {
                    if (which == 0) {
                        showControlSettingsPanel();
                    } else if (which == 1) {
                        if (!hasRemoteEndpoint() || accessToken.isEmpty()) {
                            toast("问 AI 需要已配对的 Mac");
                        } else if (!hasInternetCapability()) {
                            toast("当前离线，问 AI 暂不可用");
                        } else {
                            showHermesBookQuestionDialog();
                        }
                    } else if (which == 2) {
                        toast(syncStatusText());
                    } else if (which == 3) {
                        new AlertDialog.Builder(this)
                                .setTitle("书籍信息")
                                .setMessage(currentBookTitle + "\n\n书籍 ID\n" + currentBookId)
                                .setPositiveButton("关闭", null)
                                .show();
                    } else {
                        renderLibrary("本地缓存 " + store.books().size() + " 本 · " + lastSyncSummary);
                    }
                })
                .show();
    }

    private void showHermesBookQuestionDialog() {
        EditText question = new EditText(this);
        question.setHint("你想问这本书什么？");
        question.setMinLines(2);
        question.setMaxLines(6);
        new AlertDialog.Builder(this)
                .setTitle("问 AI")
                .setMessage("基于本书已索引正文和笔记回答；需要已配对且可达的 Mac。证据不足会明确说明。")
                .setView(question)
                .setNegativeButton("取消", null)
                .setNeutralButton("问当前句", (dialog, which) -> askHermesCurrentSentence())
                .setPositiveButton("发送", (dialog, which) ->
                        askHermesBookQuestion(question.getText().toString()))
                .show();
    }

    private void askHermesCurrentSentence() {
        String currentSentence = ReaderTtsSession.INSTANCE.currentTextForBook(currentBookId);
        if (currentSentence.isEmpty()) {
            toast("请先开始朗读；当前句入口只使用正在播放的这一句");
            return;
        }
        askHermesTextContext("当前朗读句", currentSentence);
    }

    private void askHermesBookQuestion(String rawQuestion) {
        String question = HermesBookQuestionContract.INSTANCE.cleanQuestion(rawQuestion);
        if (question.isEmpty()) {
            toast("请先输入关于这本书的问题");
            return;
        }
        boolean endpointConfigured = hasRemoteEndpoint();
        boolean credentialsConfigured = !accessToken.isEmpty();
        if (!endpointConfigured || !credentialsConfigured) {
            toast("Hermes 需要已配对的 Mac 连接");
            return;
        }
        if (HermesEvidenceStore.isPairedOffline(
                endpointConfigured,
                credentialsConfigured,
                hasInternetCapability()
        )) {
            HermesEvidenceStore.recordPairedOfflineBlocked(this, currentBookId);
            toast("当前离线；问题没有发送，也不会启动本地大模型");
            return;
        }
        String chapterTitle = currentChapterLocator;
        if (currentChapterIndex >= 0 && currentChapterIndex < currentChapters.size()) {
            chapterTitle = currentChapters.get(currentChapterIndex).title;
        }
        String title = safe(currentBookTitle).trim().isEmpty() ? "当前书籍" : safe(currentBookTitle).trim();
        String message = HermesBookQuestionContract.INSTANCE.buildMessage(
                new HermesBookQuestionContext(
                        currentBookId,
                        title,
                        safe(chapterTitle).trim(),
                        currentChapterLocator
                ),
                question
        );
        toast("Hermes 正在回答关于《" + title + "》的问题");
        executor.execute(() -> {
            try {
                JSONObject body = new JSONObject()
                        .put("message", message)
                        .put("context_scope", "current_book")
                        .put("book_id", currentBookId)
                        .put("chapter_locator", currentChapterLocator)
                        .put("question", question);
                JSONObject payload = new JSONObject(request("POST", baseUrl + "/v1/runtime/chat", body));
                if (payload.optBoolean("ok", false)) {
                    HermesEvidenceStore.recordSuccessfulReceipt(
                            this,
                            payload.optJSONObject("receipt")
                    );
                }
                String reply = payload.optString("reply", "").trim();
                if (reply.isEmpty()) {
                    reply = payload.optString("error", "Hermes 暂时没有返回内容");
                }
                final String answer = reply.replace("**", "");
                runOnUiThread(() -> {
                    if (isFinishing()) return;
                    new AlertDialog.Builder(this)
                            .setTitle("Hermes · 当前书")
                            .setMessage(answer)
                            .setPositiveButton("关闭", null)
                            .show();
                });
            } catch (Throwable error) {
                toast("Hermes 暂不可用：" + compact(error));
            }
        });
    }

    private void applyReaderVisualSettings() {
        readerPreferences = preferencesRepository.updateFallbackAppearance(
                visualFontSize,
                visualLineHeightPercent,
                visualMargin,
                visualDarkMode
        );
        if (readerWebView == null) {
            return;
        }
        readerWebView.evaluateJavascript(
                "window.__clickApplyVisualSetting && window.__clickApplyVisualSetting("
                        + visualFontSize + "," + visualLineHeightPercent + "," + visualMargin + "," + (visualDarkMode ? "true" : "false") + ");",
                null
        );
    }

    private void enqueueTextAnnotation(String operationType, String kind, String sourceText, String noteText, String color) {
        executor.execute(() -> {
            try {
                JSONObject payload = new JSONObject();
                payload.put("book_id", currentBookId);
                payload.put("kind", kind);
                payload.put("source_text", sourceText);
                payload.put("note_text", noteText);
                payload.put("color", color);
                payload.put("chapter_locator", currentChapterLocator);
                JSONObject range = new JSONObject();
                range.put("mode", "android_text_selection");
                range.put("text", sourceText);
                range.put("chapter_index", currentChapterIndex);
                payload.put("range_locator", range);
                payload.put("metadata", androidMetadata(kind));
                String opId = operationId();
                store.saveLocalAnnotationAndEnqueue(
                        opId,
                        operationType,
                        currentBookId,
                        payload
                );
                ClickSyncScheduler.enqueueMetadata(this);
                toast("已保存 · " + store.pendingCount() + " 条待同步");
            } catch (Throwable throwable) {
                toast("保存失败：" + compact(throwable));
            }
        });
    }

    private void showNoteDialog(String sourceText) {
        EditText input = new EditText(this);
        input.setMinLines(3);
        input.setTextColor(Color.BLACK);
        new AlertDialog.Builder(this)
                .setTitle("备注")
                .setMessage(sourceText)
                .setView(input)
                .setPositiveButton("保存", (dialog, which) -> enqueueTextAnnotation("note_created", "note", sourceText, input.getText().toString(), ""))
                .setNegativeButton("取消", null)
                .show();
    }

    private void chooseAudioNote(String sourceText) {
        pendingAudioNoteSourceText = sourceText == null ? "" : sourceText;
        Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
        intent.addCategory(Intent.CATEGORY_OPENABLE);
        intent.setType("audio/*");
        try {
            startActivityForResult(Intent.createChooser(intent, "选择或录制语音备注"), REQUEST_AUDIO_NOTE);
        } catch (Throwable throwable) {
            toast("无法打开音频选择器：" + compact(throwable));
        }
    }

    private void lookupWord(String selected) {
        String word = firstWord(selected);
        executor.execute(() -> {
            String cached = store.lookupCache(currentBookId, word);
            if (!cached.isEmpty()) {
                try {
                    showLookupResult(word, new JSONObject(cached), "本地缓存");
                } catch (Throwable ignored) {
                    // A damaged cache row is replaced by the network result below.
                }
            }
            if (!hasRemoteEndpoint()) {
                if (cached.isEmpty()) {
                    toast("离线状态下没有这个词的本地缓存");
                }
                return;
            }
            try {
                String url = baseUrl + "/v1/android/books/" + encode(currentBookId) + "/lookup?word=" + encode(word);
                JSONObject result = new JSONObject(request("GET", url, null));
                store.saveLookupCache(currentBookId, word, result);
                if (cached.isEmpty()) showLookupResult(word, result, "Click 词库");
            } catch (Throwable throwable) {
                if (cached.isEmpty()) toast("查词失败：" + compact(throwable));
            }
        });
    }

    private void askHermes(String selected) {
        askHermesTextContext("所选原文", selected);
    }

    private void askHermesTextContext(String contextLabel, String contextText) {
        String cleaned = safe(contextText).trim();
        if (cleaned.isEmpty()) {
            toast("请先选择要交给 Hermes 的文字");
            return;
        }
        if (!hasRemoteEndpoint() || accessToken.isEmpty()) {
            toast("Hermes 需要连接你的 Mac");
            return;
        }
        String chapterTitle = currentChapterLocator;
        if (currentChapterIndex >= 0 && currentChapterIndex < currentChapters.size()) {
            chapterTitle = currentChapters.get(currentChapterIndex).title;
        }
        final String contextChapter = safe(chapterTitle).trim().isEmpty()
                ? "当前章节"
                : safe(chapterTitle).trim();
        final String contextBook = safe(currentBookTitle).trim().isEmpty()
                ? "当前书籍"
                : safe(currentBookTitle).trim();
        toast("Hermes 正在理解" + contextLabel);
        executor.execute(() -> {
            try {
                JSONObject body = new JSONObject();
                body.put(
                        "message",
                        "你正在处理 Click 阅读器当前页面。请只依据下面明确给出的书籍、章节和原文回答，"
                                + "不要沿用会话中其他书籍的上下文。\n"
                                + "书籍：" + contextBook + "\n"
                                + "章节：" + contextChapter + "\n"
                                + contextLabel + "：" + cleaned + "\n\n"
                                + "请用简洁中文解释这段原文；证据不足时直接说明。"
                );
                JSONObject payload = new JSONObject(request("POST", baseUrl + "/v1/runtime/chat", body));
                String reply = payload.optString("reply", "").trim();
                if (reply.isEmpty()) {
                    reply = payload.optString("error", "Hermes 暂时没有返回内容");
                }
                final String answer = reply.replace("**", "");
                runOnUiThread(() -> {
                    if (isFinishing()) {
                        return;
                    }
                    new AlertDialog.Builder(this)
                            .setTitle("Hermes")
                            .setMessage(answer)
                            .setPositiveButton("关闭", null)
                            .show();
                });
            } catch (Throwable error) {
                toast("Hermes 暂不可用：" + compact(error));
            }
        });
    }

    private void showLookupResult(String word, JSONObject result, String sourceLabel) {
        JSONObject meaning = result.optJSONObject("meaning");
        String definition = meaning == null ? "" : meaning.optString("zh", "");
        if (definition.isEmpty()) definition = result.optString("context_meaning_zh", "");
        String partOfSpeech = result.optString("part_of_speech_zh", "");
        String message = (partOfSpeech + "\n" + definition + "\n" + sourceLabel).trim();
        String finalMessage = message.isEmpty() ? "未查到释义" : message;
        runOnUiThread(() -> new AlertDialog.Builder(this)
                .setTitle(word)
                .setMessage(finalMessage)
                .setNeutralButton("读词", (dialog, which) -> speakText(word, "lookup"))
                .setPositiveButton("关闭", null)
                .show());
    }

    private void speakText(String text, String kind) {
        String cleaned = safe(text).trim();
        if (cleaned.isEmpty()) {
            toast("没有可朗读内容");
            return;
        }
        ReaderTtsSession.INSTANCE.start(
                this,
                ReaderTtsTextNormalizer.INSTANCE.segment(cleaned),
                readerPreferences,
                new ReaderTtsNetworkConfig(baseUrl, deviceId, accessToken, currentBookId, currentChapterLocator)
        );
    }

    private void syncFullThenRender() {
        runEngineSync(true);
    }

    private void syncChangesThenRender() {
        runEngineSync(false);
    }

    private void runEngineSync(boolean fullBaseline) {
        if (syncInProgress) {
            toast("正在同步，请稍候");
            return;
        }
        syncInProgress = true;
        syncStage = fullBaseline ? "正在建立完整书架" : "正在同步变化";
        renderLibrary(syncStage + "...");
        executor.execute(() -> {
            ClickSyncEngine.Outcome outcome;
            try (ClickSyncEngine engine = new ClickSyncEngine(this)) {
                outcome = fullBaseline
                        ? engine.runFullForeground(stage -> showSyncStage(stage))
                        : engine.runMetadataIncremental();
            } catch (Throwable throwable) {
                outcome = ClickSyncEngine.Outcome.simple(
                        ClickSyncEngine.Outcome.Status.PERMANENT_FAILURE,
                        compact(throwable)
                );
            }
            if (outcome.assetsRemain
                    && outcome.status != ClickSyncEngine.Outcome.Status.AUTH_REQUIRED
                    && outcome.status != ClickSyncEngine.Outcome.Status.PERMANENT_FAILURE) {
                ClickSyncScheduler.enqueueAssets(this);
            }
            if (outcome.metadataRepairsRemain
                    && outcome.status != ClickSyncEngine.Outcome.Status.AUTH_REQUIRED
                    && outcome.status != ClickSyncEngine.Outcome.Status.PERMANENT_FAILURE) {
                ClickSyncScheduler.replaceMetadataRepairFromStore(this);
            }
            String summary = humanSyncOutcome(outcome, fullBaseline);
            runOnUiThread(() -> {
                syncInProgress = false;
                syncStage = "就绪";
                lastSyncSummary = summary;
                renderLibrary(summary);
            });
        });
    }

    private void showSyncStage(String stage) {
        String message;
        if ("upload_operations".equals(stage)) {
            message = "正在同步手机端变更...";
        } else if ("download_full_metadata".equals(stage)) {
            message = "正在获取完整书架...";
        } else if ("commit_full_baseline".equals(stage)) {
            message = "正在保存完整书架...";
        } else {
            return;
        }
        syncStage = message;
        runOnUiThread(() -> renderLibrary(message));
    }

    private String humanSyncOutcome(ClickSyncEngine.Outcome outcome, boolean fullBaseline) {
        switch (outcome.status) {
            case SUCCESS:
                String success = fullBaseline ? "完整书架同步完成" : "书架变化同步完成";
                if (outcome.booksFailed > 0) {
                    success += "；" + outcome.booksFailed + " 本书暂时失败";
                }
                if (outcome.assetsRemain) {
                    success += "；资源将在不限流量网络继续缓存";
                }
                return success;
            case BUSY:
                return "后台同步正在进行；当前仍可离线阅读";
            case RETRYABLE_FAILURE:
                return "远端变化已保存；未完成项目会在联网后自动重试";
            case AUTH_REQUIRED:
                return "Mac 授权已失效，请重新配对";
            case NOT_CONFIGURED:
                return "离线阅读 · 尚未完成 Mac 配对";
            case PERMANENT_FAILURE:
            default:
                String detail = safe(outcome.message).trim();
                return detail.isEmpty() ? "同步协议异常" : "同步协议异常：" + detail;
        }
    }

    private void enqueuePosition(ClickNativeStore.ChapterRow chapter) {
        try {
            JSONObject locator = new JSONObject();
            locator.put("type", "readium_locator_pending");
            locator.put("chapter_index", chapter.index);
            locator.put("locator", chapter.locator);
            JSONObject payload = new JSONObject();
            payload.put("book_id", currentBookId);
            payload.put("chapter_locator", chapter.locator);
            payload.put("page_index", chapter.index);
            payload.put("total_pages", Math.max(1, currentChapters.size()));
            payload.put("page_ratio", currentChapters.isEmpty() ? 0.0 : (double) chapter.index / Math.max(1, currentChapters.size()));
            payload.put("locator", locator);
            store.saveLocalPositionAndEnqueueLatest(
                    positionOperationId(),
                    currentBookId,
                    payload
            );
            ClickSyncScheduler.enqueueMetadata(this);
        } catch (Throwable ignored) {
            // Reading should not be blocked by position persistence.
        }
    }

    private JSONObject androidMetadata(String action) throws Exception {
        JSONObject metadata = new JSONObject();
        metadata.put("source", "click_android_native_reader");
        metadata.put("readium_toolkit_dependency", READIUM_TOOLKIT_DEPENDENCY);
        metadata.put("device_id", deviceId);
        metadata.put("action", action);
        metadata.put("chapter_index", currentChapterIndex);
        metadata.put("conflict_policy", "updated_at_device_id_operation_id");
        return metadata;
    }

    private String request(String method, String rawUrl, JSONObject body) throws Exception {
        if (!hasRemoteEndpoint()) {
            throw new IllegalStateException("Mac Reader API 当前不可用");
        }
        String safeUrl = ReaderApiUrlPolicy.resolve(baseUrl, rawUrl);
        HttpURLConnection connection = (HttpURLConnection) new URL(safeUrl).openConnection();
        connection.setInstanceFollowRedirects(false);
        connection.setConnectTimeout(8000);
        connection.setReadTimeout(180000);
        connection.setRequestMethod(method);
        connection.setRequestProperty("Accept", "application/json");
        connection.setRequestProperty("X-Click-Device-Id", deviceId);
        if (!accessToken.isEmpty()) {
            connection.setRequestProperty("X-Click-Access-Token", accessToken);
            connection.setRequestProperty("Authorization", "Bearer " + accessToken);
        }
        if (body != null) {
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(bytes);
            }
        }
        int code = connection.getResponseCode();
        String text = readConnectionText(connection, code);
        connection.disconnect();
        if (code < 200 || code >= 300) {
            throw new IllegalStateException("HTTP " + code + " " + text);
        }
        return text;
    }

    private String readConnectionText(HttpURLConnection connection, int code) throws Exception {
        InputStream stream = code >= 200 && code < 300 ? connection.getInputStream() : connection.getErrorStream();
        if (stream == null) {
            return "";
        }
        try (InputStream input = stream; ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                output.write(buffer, 0, read);
            }
            return output.toString("UTF-8");
        }
    }

    private byte[] readAllBytes(Uri uri) throws Exception {
        try (InputStream input = getContentResolver().openInputStream(uri);
             ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            if (input == null) {
                return new byte[0];
            }
            byte[] buffer = new byte[8192];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                output.write(buffer, 0, read);
            }
            return output.toByteArray();
        }
    }

    private String extractSpeakableText() {
        return currentPageSpeakText;
    }

    private void copyText(String text) {
        ClipboardManager manager = (ClipboardManager) getSystemService(Context.CLIPBOARD_SERVICE);
        if (manager != null) {
            manager.setPrimaryClip(ClipData.newPlainText("Click selection", text));
            toast("已复制");
        }
    }

    private String firstWord(String selected) {
        String trimmed = safe(selected).trim();
        int index = trimmed.indexOf(' ');
        return index <= 0 ? trimmed : trimmed.substring(0, index);
    }

    private String operationId() {
        return "android-" + UUID.randomUUID();
    }

    private String positionOperationId() {
        String safeDevice = safe(deviceId).replaceAll("[^A-Za-z0-9._-]", "_");
        return "android-position-" + (safeDevice.isEmpty() ? "device" : safeDevice) + "-" + UUID.randomUUID();
    }

    private String cleanBaseUrl(String value) {
        String raw = safe(value).trim();
        while (raw.endsWith("/")) {
            raw = raw.substring(0, raw.length() - 1);
        }
        return raw;
    }

    private boolean hasRemoteEndpoint() {
        return !baseUrl.trim().isEmpty();
    }

    static String initialLibrarySummary(
            boolean hasRemoteEndpoint,
            boolean hasInternetCapability
    ) {
        if (!hasRemoteEndpoint) {
            return "离线阅读 · 尚未完成 Mac 配对";
        }
        return hasInternetCapability
                ? "本地书架 · 正在后台同步"
                : "离线阅读";
    }

    private boolean hasInternetCapability() {
        ConnectivityManager manager =
                (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (manager == null) {
            return false;
        }
        try {
            Network activeNetwork = manager.getActiveNetwork();
            if (activeNetwork == null) {
                return false;
            }
            NetworkCapabilities capabilities =
                    manager.getNetworkCapabilities(activeNetwork);
            return capabilities != null
                    && capabilities.hasCapability(
                            NetworkCapabilities.NET_CAPABILITY_INTERNET
                    );
        } catch (RuntimeException ignored) {
            return false;
        }
    }

    private String statusLine(String status) {
        return "Readium P1 / 本地副本 / 待同步 " + store.pendingCount() + " · " + safe(status);
    }

    private String mimeType(Uri uri) {
        String value = getContentResolver().getType(uri);
        return value == null || value.trim().isEmpty() ? "audio/*" : value;
    }

    private void toast(String message) {
        runOnUiThread(() -> {
            if (isFinishing() || isDestroyed()) {
                return;
            }
            Toast.makeText(this, message, Toast.LENGTH_LONG).show();
        });
    }

    private String compact(Throwable throwable) {
        String message = throwable.getMessage();
        if (message == null || message.trim().isEmpty()) {
            message = throwable.getClass().getSimpleName();
        }
        return message.length() > 120 ? message.substring(0, 120) : message;
    }

    private String plainText(String html) {
        String text = safe(html)
                .replaceAll("(?is)<script.*?</script>", " ")
                .replaceAll("(?is)<style.*?</style>", " ")
                .replaceAll("(?is)<[^>]+>", " ")
                .replace("&nbsp;", " ")
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", "\"")
                .replaceAll("\\s+", " ")
                .trim();
        return text.length() > 2600 ? text.substring(0, 2600) : text;
    }

    private String encode(String value) throws Exception {
        return URLEncoder.encode(safe(value), "UTF-8");
    }

    private String safe(String value) {
        return value == null ? "" : value;
    }

    private String escapeHtml(String value) {
        return safe(value)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\"", "&quot;");
    }

    private String readerBgColor() {
        return visualDarkMode ? "#080807" : "#eeeade";
    }

    private String readerTextColor() {
        return visualDarkMode ? "#f6f6f1" : "#1f211f";
    }

    private String readerMutedColor() {
        return visualDarkMode ? "#8d9188" : "#686e63";
    }

    private String syncStatusText() {
        String stage = syncInProgress ? syncStage : lastSyncSummary;
        int pending = pendingSyncItemCount(
                store.pendingCount(),
                store.pendingBookImportCount()
        );
        return safe(stage) + " · 待同步 " + pending;
    }

    static int pendingSyncItemCount(int operationCount, int bookImportCount) {
        return Math.max(0, operationCount) + Math.max(0, bookImportCount);
    }

    private LinearLayout moonToolbar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(8), dp(6), dp(8), dp(6));
        bar.setBackgroundColor(ClickUi.canvas(this, visualDarkMode));
        return bar;
    }

    private Button toolbarButton(String label, View.OnClickListener listener) {
        Button button = smallButton(label, listener);
        button.setTextSize(16);
        button.setTextColor(ClickUi.primaryLabel(this, visualDarkMode));
        button.setMinWidth(0);
        button.setMinimumWidth(0);
        button.setMinHeight(0);
        button.setMinimumHeight(0);
        button.setPadding(0, 0, 0, 0);
        button.setLayoutParams(new LinearLayout.LayoutParams(dp(48), ViewGroup.LayoutParams.MATCH_PARENT));
        button.setBackground(ClickUi.ripple(
                Color.TRANSPARENT,
                dp(24),
                Color.TRANSPARENT,
                0,
                ClickUi.accent(this, visualDarkMode)
        ));
        return button;
    }

    private Button toolbarIconButton(
            int iconResource,
            String description,
            View.OnClickListener listener
    ) {
        Button button = smallButton("", listener);
        ClickUi.styleIconButton(button, iconResource, description, visualDarkMode);
        button.setLayoutParams(new LinearLayout.LayoutParams(dp(48), ViewGroup.LayoutParams.MATCH_PARENT));
        return button;
    }

    private Button compactBlueButton(String label, View.OnClickListener listener) {
        Button button = smallButton(label, listener);
        ClickUi.stylePrimary(button, visualDarkMode);
        button.setTextSize(14);
        button.setMinWidth(dp(72));
        button.setMinHeight(dp(40));
        return button;
    }

    private Button tinyBookButton(String label, View.OnClickListener listener) {
        Button button = smallButton(label, listener);
        button.setTextSize(11);
        button.setTextColor(Color.rgb(88, 67, 86));
        button.setMinWidth(0);
        button.setMinimumWidth(0);
        button.setMinHeight(0);
        button.setMinimumHeight(0);
        button.setPadding(0, 0, 0, 0);
        button.setLayoutParams(new LinearLayout.LayoutParams(dp(40), dp(30)));
        button.setBackgroundColor(Color.TRANSPARENT);
        return button;
    }

    private GridLayout.LayoutParams bookTileParams() {
        GridLayout.LayoutParams params = new GridLayout.LayoutParams();
        params.width = getResources().getDisplayMetrics().widthPixels / 3 - dp(8);
        params.height = ViewGroup.LayoutParams.WRAP_CONTENT;
        params.setMargins(0, 0, 0, dp(12));
        return params;
    }

    private String coverLabel(String title) {
        String clean = safe(title).replaceAll("\\s+", "");
        if (clean.length() <= 6) {
            return clean.isEmpty() ? "Click" : clean;
        }
        return clean.substring(0, Math.min(8, clean.length()));
    }

    private int bookCoverColor(String title) {
        int hash = Math.abs(safe(title).hashCode());
        int[] colors = {
                Color.rgb(224, 230, 218),
                Color.rgb(216, 221, 200),
                Color.rgb(227, 213, 190),
                Color.rgb(206, 220, 211),
                Color.rgb(224, 220, 232),
                Color.rgb(198, 214, 225)
        };
        return colors[hash % colors.length];
    }

    private TextView drawerRow(String label) {
        TextView row = text(label, 18, ClickUi.primaryLabel(this, visualDarkMode));
        row.setTypeface(Typeface.DEFAULT_BOLD);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(8), 0, dp(8), 0);
        return row;
    }

    private TextView filterRow(String label) {
        TextView row = text(label, 17, ClickUi.primaryLabel(this, visualDarkMode));
        row.setTypeface(Typeface.DEFAULT_BOLD);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, 0, 0, 0);
        return row;
    }

    private Button drawerSmallAction(String label, View.OnClickListener listener) {
        Button button = smallButton(label, listener);
        button.setTextSize(12);
        button.setTextColor(Color.rgb(87, 64, 83));
        button.setBackgroundColor(Color.TRANSPARENT);
        return button;
    }

    private void closeParentPopup(View view) {
        View current = view;
        while (current != null) {
            Object tag = current.getTag();
            if (tag instanceof PopupWindow) {
                ((PopupWindow) tag).dismiss();
                return;
            }
            if (!(current.getParent() instanceof View)) {
                return;
            }
            current = (View) current.getParent();
        }
    }

    private LinearLayout settingsPanel(String title) {
        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dp(18), dp(14), dp(18), dp(18));
        panel.setBackground(ClickUi.rounded(
                ClickUi.surface(this, visualDarkMode),
                dp(24),
                ClickUi.surface(this, visualDarkMode),
                0
        ));
        TextView heading = text(title, 22, ClickUi.primaryLabel(this, visualDarkMode));
        heading.setTypeface(Typeface.DEFAULT_BOLD);
        heading.setGravity(Gravity.CENTER_VERTICAL);
        panel.addView(heading, new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(48)));
        return panel;
    }

    private View settingSeekRow(String label, int min, int max, int value, IntValueSetter setter) {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(0, dp(8), 0, dp(8));
        TextView name = text(label, 16, ClickUi.primaryLabel(this, visualDarkMode));
        name.setTypeface(Typeface.DEFAULT_BOLD);
        row.addView(name, new LinearLayout.LayoutParams(dp(92), ViewGroup.LayoutParams.WRAP_CONTENT));
        SeekBar seek = new SeekBar(this);
        seek.setMax(Math.max(1, max - min));
        seek.setProgress(Math.max(0, Math.min(max - min, value - min)));
        row.addView(seek, new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1));
        TextView number = text(String.valueOf(value), 15, ClickUi.secondaryLabel(this, visualDarkMode));
        number.setGravity(Gravity.CENTER);
        row.addView(number, new LinearLayout.LayoutParams(dp(46), ViewGroup.LayoutParams.WRAP_CONTENT));
        seek.setOnSeekBarChangeListener(new SeekBar.OnSeekBarChangeListener() {
            @Override
            public void onProgressChanged(SeekBar seekBar, int progress, boolean fromUser) {
                int next = min + progress;
                number.setText(String.valueOf(next));
                setter.set(next);
            }

            @Override
            public void onStartTrackingTouch(SeekBar seekBar) {
            }

            @Override
            public void onStopTrackingTouch(SeekBar seekBar) {
            }
        });
        return row;
    }

    private Button settingsButton(String label, View.OnClickListener listener) {
        Button button = compactBlueButton(label, listener);
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(44));
        params.setMargins(0, dp(8), 0, 0);
        button.setLayoutParams(params);
        return button;
    }

    private void showBottomSheetLike(LinearLayout panel) {
        ScrollView scroll = new ScrollView(this);
        scroll.addView(panel);
        PopupWindow popup = new PopupWindow(scroll, ViewGroup.LayoutParams.MATCH_PARENT, Math.round(getResources().getDisplayMetrics().heightPixels * 0.74f), true);
        popup.setOutsideTouchable(true);
        popup.showAtLocation(root, Gravity.BOTTOM, 0, 0);
        panel.setTag(popup);
        scroll.setTag(popup);
    }

    private LinearLayout.LayoutParams weightWrapParams() {
        return new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1);
    }

    private interface IntValueSetter {
        void set(int value);
    }

    private LinearLayout horizontalBar() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(8), dp(5), dp(8), dp(5));
        bar.setBackgroundColor(ClickUi.surface(this, visualDarkMode));
        return bar;
    }

    private LinearLayout compactHeader() {
        LinearLayout bar = new LinearLayout(this);
        bar.setOrientation(LinearLayout.HORIZONTAL);
        bar.setGravity(Gravity.CENTER_VERTICAL);
        bar.setPadding(dp(8), dp(5), dp(8), dp(5));
        bar.setBackgroundColor(ClickUi.canvas(this, visualDarkMode));
        return bar;
    }

    private Button smallButton(String label, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setTextSize(13);
        button.setOnClickListener(listener);
        return button;
    }

    private Button pillButton(String label, View.OnClickListener listener) {
        Button button = smallButton(label, listener);
        ClickUi.stylePill(button, false, visualDarkMode);
        button.setMinWidth(dp(62));
        return button;
    }

    private Button primaryPillButton(String label, View.OnClickListener listener) {
        Button button = smallButton(label, listener);
        ClickUi.stylePrimary(button, visualDarkMode);
        return button;
    }

    private Button listButton(String label, View.OnClickListener listener) {
        Button button = smallButton(label, listener);
        ClickUi.styleSecondaryButton(button, visualDarkMode);
        return button;
    }

    private GradientDrawable rounded(int color, int radius, int strokeColor, int strokeWidth) {
        GradientDrawable drawable = new GradientDrawable();
        drawable.setColor(color);
        drawable.setCornerRadius(radius);
        if (strokeWidth > 0) {
            drawable.setStroke(strokeWidth, strokeColor);
        }
        return drawable;
    }

    private TextView text(String value, int sp, int color) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sp);
        view.setTextColor(color);
        return view;
    }

    private TextView smallText(String value, int sp, int color) {
        TextView view = text(value, sp, color);
        view.setGravity(Gravity.CENTER_VERTICAL);
        view.setSingleLine(false);
        return view;
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
    }

    private LinearLayout.LayoutParams matchFixedHeight(int heightDp) {
        return new LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(heightDp));
    }

    private LinearLayout.LayoutParams weightParams() {
        return new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.MATCH_PARENT, 1);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }
}
