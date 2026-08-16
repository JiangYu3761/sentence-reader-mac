package com.click.shell;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Typeface;
import android.net.Uri;
import android.os.Bundle;
import android.view.Gravity;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Minimal Android file-picker and "Open with Click" front end.
 *
 * <p>Manifest and shelf wiring are intentionally separate from this ingress checkpoint.</p>
 */
public final class BookImportActivity extends Activity {
    static final String EXTRA_LOCAL_BOOK_ID = "click.extra.LOCAL_BOOK_ID";
    static final String EXTRA_SOURCE_KIND = "click.extra.SOURCE_KIND";
    static final String EXTRA_SOURCE_HASH = "click.extra.SOURCE_HASH";
    static final String EXTRA_SOURCE_BYTE_SIZE = "click.extra.SOURCE_BYTE_SIZE";

    private static final int REQUEST_OPEN_BOOK = 4101;
    private static final String STATE_PICKER_LAUNCHED = "picker_launched";

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private TextView status;
    private Button chooseButton;
    private Button doneButton;
    private boolean importInProgress;
    private boolean pickerLaunched;
    private boolean interfaceDarkMode = true;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        interfaceDarkMode = new ReaderPreferencesRepository(this).load().getTheme() == ReaderTheme.NIGHT;
        ClickUi.applySystemBars(this, interfaceDarkMode);
        pickerLaunched = savedInstanceState != null
                && savedInstanceState.getBoolean(STATE_PICKER_LAUNCHED, false);
        buildPage();
        if (!handleIncoming(getIntent()) && !pickerLaunched) {
            openPicker();
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleIncoming(intent);
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        outState.putBoolean(STATE_PICKER_LAUNCHED, pickerLaunched);
        super.onSaveInstanceState(outState);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQUEST_OPEN_BOOK) {
            return;
        }
        pickerLaunched = false;
        Uri uri = resultCode == RESULT_OK && data != null ? data.getData() : null;
        if (uri == null) {
            showIdle("没有选择文件。你可以重新选择 EPUB 或 PDF。");
            return;
        }
        beginImport(uri);
    }

    @Override
    protected void onDestroy() {
        executor.shutdownNow();
        super.onDestroy();
    }

    private void buildPage() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER_HORIZONTAL);
        root.setPadding(dp(28), dp(48), dp(28), dp(32));
        root.setBackgroundColor(ClickUi.canvas(this, interfaceDarkMode));

        TextView title = text("导入到 Click", 28, ClickUi.primaryLabel(this, interfaceDarkMode));
        title.setTypeface(Typeface.DEFAULT_BOLD);
        root.addView(title, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        status = text(
                "选择一份 EPUB 或 PDF。原书会先安全保存在手机，再等待归入 Mac 主库。",
                16,
                ClickUi.secondaryLabel(this, interfaceDarkMode)
        );
        status.setPadding(0, dp(22), 0, dp(28));
        root.addView(status, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        chooseButton = button("选择 EPUB / PDF");
        ClickUi.stylePrimary(chooseButton, interfaceDarkMode);
        chooseButton.setOnClickListener(view -> openPicker());
        root.addView(chooseButton, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(52)
        ));

        doneButton = button("完成");
        ClickUi.styleSecondaryButton(doneButton, interfaceDarkMode);
        doneButton.setVisibility(Button.GONE);
        doneButton.setOnClickListener(view -> finish());
        LinearLayout.LayoutParams doneParams = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(52)
        );
        doneParams.topMargin = dp(12);
        root.addView(doneButton, doneParams);
        setContentView(root);
    }

    private boolean handleIncoming(Intent intent) {
        if (intent == null) {
            return false;
        }
        String action = intent.getAction();
        Uri uri = intent.getData();
        boolean importAction = Intent.ACTION_VIEW.equals(action)
                || Intent.ACTION_OPEN_DOCUMENT.equals(action);
        if (!importAction || uri == null) {
            return false;
        }
        if (!"content".equalsIgnoreCase(uri.getScheme())) {
            showIdle("Click 只接收 Android 安全授权的 EPUB / PDF 文件，请重新选择。");
            return true;
        }
        beginImport(uri);
        return true;
    }

    private void openPicker() {
        if (importInProgress) {
            return;
        }
        Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        picker.addCategory(Intent.CATEGORY_OPENABLE);
        picker.setType("*/*");
        picker.putExtra(
                Intent.EXTRA_MIME_TYPES,
                new String[]{
                        "application/epub+zip",
                        "application/pdf",
                        "application/zip",
                        "application/octet-stream"
                }
        );
        picker.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        pickerLaunched = true;
        try {
            startActivityForResult(picker, REQUEST_OPEN_BOOK);
        } catch (Throwable error) {
            pickerLaunched = false;
            showIdle("系统文件选择器无法打开，请从文件 App 里选择“用 Click 打开”。");
        }
    }

    private void beginImport(Uri uri) {
        if (importInProgress) {
            return;
        }
        importInProgress = true;
        chooseButton.setEnabled(false);
        doneButton.setVisibility(Button.GONE);
        status.setText("正在校验并保存原书…\n请暂时不要关闭 Click。");
        executor.execute(() -> {
            try {
                BookImportIngress.Result imported =
                        new BookImportIngress(this).importContent(uri);
                runOnUiThread(() -> showSuccess(imported));
            } catch (BookImportIngress.ImportException error) {
                String message = error.getMessage() == null
                        ? "导入失败，请重新选择 EPUB 或 PDF"
                        : error.getMessage();
                runOnUiThread(() -> showIdle(message));
            } catch (Throwable error) {
                runOnUiThread(() -> showIdle("导入失败，请重新选择 EPUB 或 PDF"));
            }
        });
    }

    private void showSuccess(BookImportIngress.Result imported) {
        if (isFinishing() || isDestroyed()) {
            return;
        }
        importInProgress = false;
        chooseButton.setEnabled(true);
        doneButton.setVisibility(Button.VISIBLE);
        String kind = "pdf".equals(imported.sourceKind) ? "PDF" : "EPUB";
        String syncLine = imported.uploadWakeScheduled
                ? "联网且 Mac 可达时会自动归入主库。"
                : "书和待归档记录已保存；下次联网或打开 Click 时会继续归档。";
        status.setText(
                "已保存《" + imported.title + "》(" + kind + ")。\n"
                        + "现在断网或重启手机，原书仍保留在 Click 里。\n"
                        + syncLine
        );
        Intent result = new Intent();
        result.putExtra(EXTRA_LOCAL_BOOK_ID, imported.localBookId);
        result.putExtra(EXTRA_SOURCE_KIND, imported.sourceKind);
        result.putExtra(EXTRA_SOURCE_HASH, imported.sourceHash);
        result.putExtra(EXTRA_SOURCE_BYTE_SIZE, imported.sourceByteSize);
        setResult(RESULT_OK, result);
    }

    private void showIdle(String message) {
        if (isFinishing() || isDestroyed()) {
            return;
        }
        importInProgress = false;
        chooseButton.setEnabled(true);
        doneButton.setVisibility(Button.VISIBLE);
        status.setText(message);
    }

    private TextView text(String value, int sizeSp, int color) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sizeSp);
        view.setTextColor(color);
        view.setLineSpacing(0f, 1.18f);
        return view;
    }

    private Button button(String label) {
        Button view = new Button(this);
        view.setText(label);
        view.setTextSize(16);
        return view;
    }

    private int dp(int value) {
        return Math.round(
                value * getResources().getDisplayMetrics().density
        );
    }
}
