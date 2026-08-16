package com.click.shell;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.pm.PackageManager;
import android.content.Intent;
import android.graphics.Color;
import android.graphics.Typeface;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.os.Bundle;
import android.text.InputFilter;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.BaseAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ListView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * Offline-first front end for the file-backed Tingle capture repository.
 *
 * <p>This Activity deliberately owns no database and never deletes original.wav. The Mac
 * inspiration workbench remains a secondary online destination.</p>
 */
public final class TingleLocalActivity extends Activity {
    private static final int REQUEST_RECORD_AUDIO = 2001;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private final CaptureAdapter adapter = new CaptureAdapter();

    private TingleCaptureRepository repository;
    private TextView summary;
    private ListView list;
    private MediaPlayer player;
    private String playingCaptureId = "";
    private boolean loadInProgress;
    private Button recordButton;
    private ClickWavRecorder wavRecorder;
    private String recordingCaptureId = "";
    private boolean recordingStopInProgress;
    private boolean interfaceDarkMode = true;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        repository = new TingleCaptureRepository(this);
        interfaceDarkMode = new ReaderPreferencesRepository(this).load().getTheme() == ReaderTheme.NIGHT;
        ClickUi.applySystemBars(this, interfaceDarkMode);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(ClickUi.canvas(this, interfaceDarkMode));

        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setOrientation(LinearLayout.HORIZONTAL);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(dp(8), dp(6), dp(8), dp(6));
        toolbar.setBackgroundColor(ClickUi.canvas(this, interfaceDarkMode));

        Button back = toolbarIconButton(R.drawable.ic_click_back, "返回");
        back.setOnClickListener(view -> finish());
        toolbar.addView(back, new LinearLayout.LayoutParams(dp(48), dp(48)));

        TextView title = text("Tingle", 24, ClickUi.primaryLabel(this, interfaceDarkMode));
        ClickUi.styleNavigationTitle(title, interfaceDarkMode);
        title.setTextSize(24);
        toolbar.addView(title, new LinearLayout.LayoutParams(0, dp(48), 1));

        Button refresh = toolbarIconButton(R.drawable.ic_click_refresh, "刷新本地录音");
        refresh.setOnClickListener(view -> loadCaptures());
        toolbar.addView(refresh, new LinearLayout.LayoutParams(dp(48), dp(48)));

        recordButton = toolbarButton("录音");
        recordButton.setTextSize(13);
        ClickUi.stylePrimary(recordButton, interfaceDarkMode);
        recordButton.setContentDescription("开始 Tingle 录音");
        recordButton.setOnClickListener(view -> toggleRecording());
        toolbar.addView(recordButton, new LinearLayout.LayoutParams(dp(58), dp(48)));

        Button online = toolbarButton("处理结果");
        online.setTextSize(13);
        ClickUi.styleSecondaryButton(online, interfaceDarkMode);
        online.setContentDescription("查看 Mac 已处理灵感");
        online.setOnClickListener(view -> openMacHistory());
        toolbar.addView(online, new LinearLayout.LayoutParams(dp(76), dp(48)));
        root.addView(toolbar, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(60)
        ));

        summary = text("正在读取手机里的录音…", 14, ClickUi.secondaryLabel(this, interfaceDarkMode));
        summary.setPadding(dp(16), dp(12), dp(16), dp(12));
        root.addView(summary, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        list = new ListView(this);
        list.setAdapter(adapter);
        list.setDividerHeight(1);
        list.setDivider(ClickUi.rounded(
                ClickUi.separator(this, interfaceDarkMode),
                0,
                ClickUi.separator(this, interfaceDarkMode),
                0
        ));
        list.setBackgroundColor(ClickUi.canvas(this, interfaceDarkMode));
        list.setOnItemClickListener((parent, view, position, id) ->
                showCapture(adapter.getItem(position)));
        TextView empty = text(
                "还没有本地 Tingle 录音。\n录音完成后，原音会保留在手机，并在联网时自动同步。",
                16,
                ClickUi.secondaryLabel(this, interfaceDarkMode)
        );
        empty.setGravity(Gravity.CENTER);
        empty.setPadding(dp(28), dp(52), dp(28), dp(52));
        root.addView(empty, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));
        list.setEmptyView(empty);
        root.addView(list, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                0,
                1
        ));
        setContentView(root);
        ViewCompat.setOnApplyWindowInsetsListener(root, (view, insets) -> {
            Insets systemBars = insets.getInsets(WindowInsetsCompat.Type.systemBars());
            view.setPadding(
                    systemBars.left,
                    systemBars.top,
                    systemBars.right,
                    systemBars.bottom
            );
            return insets;
        });
        ViewCompat.requestApplyInsets(root);
    }

    @Override
    protected void onResume() {
        super.onResume();
        loadCaptures();
    }

    @Override
    protected void onDestroy() {
        releasePlayer();
        ClickWavRecorder activeRecorder = wavRecorder;
        wavRecorder = null;
        if (activeRecorder != null) {
            activeRecorder.close();
        }
        executor.shutdownNow();
        super.onDestroy();
    }

    @Override
    public void onRequestPermissionsResult(
            int requestCode,
            String[] permissions,
            int[] grantResults
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != REQUEST_RECORD_AUDIO) {
            return;
        }
        boolean granted = grantResults.length > 0
                && grantResults[0] == PackageManager.PERMISSION_GRANTED;
        if (granted) {
            startRecording();
        } else {
            toast("没有麦克风权限，未创建录音");
        }
    }

    private void loadCaptures() {
        if (loadInProgress) {
            return;
        }
        loadInProgress = true;
        executor.execute(() -> {
            try {
                repository.recoverInterruptedCaptures();
                if (repository.pendingCount() > 0 && ClickSyncConfig.load(this).isReady()) {
                    TingleUploadScheduler.enqueue(this);
                }
            } catch (Throwable ignored) {
                // The original WAV stays private; another app event can retry recovery.
            }
            List<TingleCaptureRepository.Capture> captures =
                    new ArrayList<>(repository.listCaptures());
            Collections.reverse(captures);
            boolean configured = ClickSyncConfig.load(this).isReady();
            runOnUiThread(() -> {
                loadInProgress = false;
                adapter.replace(captures, configured);
                int pending = 0;
                for (TingleCaptureRepository.Capture capture : captures) {
                    if (!TingleCaptureRepository.SYNC_SYNCED.equals(capture.syncState)) {
                        pending += 1;
                    }
                }
                summary.setText(
                        captures.isEmpty()
                                ? "手机本地记录"
                                : captures.size() + " 条本地录音 · "
                                + (pending == 0 ? "均已同步" : pending + " 条尚未同步")
                                + "\n点开录音可播放或编辑标题和备注；原音不会在这里删除。"
                );
            });
        });
    }

    private void toggleRecording() {
        if (recordingStopInProgress) {
            return;
        }
        if (wavRecorder != null && wavRecorder.isRecording()) {
            stopRecording();
            return;
        }
        if (android.os.Build.VERSION.SDK_INT >= 23
                && checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(
                    new String[]{Manifest.permission.RECORD_AUDIO},
                    REQUEST_RECORD_AUDIO
            );
            return;
        }
        startRecording();
    }

    private void startRecording() {
        if (wavRecorder != null || recordingStopInProgress) {
            return;
        }
        String captureId = "";
        try {
            TingleCaptureRepository.Capture capture =
                    repository.beginCapture(ClickSyncConfig.ensureDeviceId(this));
            captureId = capture.captureId;
            ClickWavRecorder next = new ClickWavRecorder(
                    () -> runOnUiThread(this::stopRecordingAtSizeLimit)
            );
            next.start(capture.audioFile);
            wavRecorder = next;
            recordingCaptureId = capture.captureId;
            recordButton.setText("停止");
            recordButton.setContentDescription("停止并保存 Tingle 录音");
            summary.setText("正在录音 · 点“停止”后原音先保存在手机");
        } catch (Throwable error) {
            if (!captureId.isEmpty()) {
                try {
                    repository.markRecordingFailure(captureId, error);
                } catch (Throwable ignored) {
                    // The private capture directory remains available for recovery.
                }
            }
            toast("录音启动失败：" + compact(error));
        }
    }

    private void stopRecording() {
        ClickWavRecorder activeRecorder = wavRecorder;
        String captureId = recordingCaptureId;
        if (activeRecorder == null || captureId.isEmpty()) {
            return;
        }
        wavRecorder = null;
        recordingCaptureId = "";
        recordingStopInProgress = true;
        recordButton.setEnabled(false);
        summary.setText("正在保存原始音频…");
        TingleCaptureRepository.Capture savedCapture = null;
        try {
            ClickWavRecorder.Result result = activeRecorder.stopAndFinalize();
            savedCapture = repository.markReady(captureId, result.durationSeconds);
            TingleUploadScheduler.enqueue(this);
            toast("原音已保存在手机");
        } catch (Throwable error) {
            try {
                repository.markRecordingFailure(captureId, error);
            } catch (Throwable ignored) {
                // Never delete the private Tingle directory after a failed finalize.
            }
            toast("录音保存失败：" + compact(error));
        }
        finishRecordingUi();
        loadCaptures();
        if (savedCapture != null) {
            showCapture(savedCapture, true);
        }
    }

    private void stopRecordingAtSizeLimit() {
        if (wavRecorder == null || recordingStopInProgress) {
            return;
        }
        toast("录音已达稳定上限，为保证稳定已保存，请另录一段");
        stopRecording();
    }

    private void finishRecordingUi() {
        recordingStopInProgress = false;
        recordButton.setEnabled(true);
        recordButton.setText("录音");
        recordButton.setContentDescription("开始 Tingle 录音");
    }

    private void showCapture(TingleCaptureRepository.Capture capture) {
        showCapture(capture, false);
    }

    private void showCapture(
            TingleCaptureRepository.Capture capture,
            boolean justRecorded
    ) {
        boolean configured = ClickSyncConfig.load(this).isReady();
        String status = statusText(capture, configured);
        String verificationCode =
                TingleCaptureRepository.verificationCode(capture.captureId);
        String message = capture.note.isEmpty() ? "没有备注" : capture.note;
        if (!capture.lastError.isEmpty()) {
            message += "\n\n最近问题：" + capture.lastError;
        }
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle(
                        (justRecorded ? "本轮刚录 · " : "")
                                + status
                                + " · 录音码 "
                                + verificationCode
                )
                .setMessage(
                        displayTitle(capture)
                                + " · " + formatDuration(capture.durationSeconds)
                                + "\n" + displayTime(capture)
                                + "\n\n" + message
                )
                .setPositiveButton(
                        capture.captureId.equals(playingCaptureId) ? "停止" : "播放",
                        (ignored, which) -> togglePlayback(capture)
                )
                .setNeutralButton("编辑", (ignored, which) -> showEditDialog(capture))
                .setNegativeButton("关闭", null)
                .create();
        dialog.setOnShowListener(ignored ->
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(capture.hasPlayableAudio()));
        dialog.show();
    }

    private void showEditDialog(TingleCaptureRepository.Capture capture) {
        LinearLayout fields = new LinearLayout(this);
        fields.setOrientation(LinearLayout.VERTICAL);
        fields.setPadding(dp(20), dp(8), dp(20), 0);

        EditText title = new EditText(this);
        title.setHint("标题");
        title.setSingleLine(true);
        title.setFilters(new InputFilter[]{new InputFilter.LengthFilter(160)});
        title.setText(capture.title);
        fields.addView(title, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        EditText note = new EditText(this);
        note.setHint("备注");
        note.setMinLines(3);
        note.setMaxLines(7);
        note.setGravity(Gravity.TOP);
        note.setFilters(new InputFilter[]{new InputFilter.LengthFilter(4000)});
        note.setText(capture.note);
        fields.addView(note, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        new AlertDialog.Builder(this)
                .setTitle("编辑基本信息")
                .setView(fields)
                .setPositiveButton("保存", (dialog, which) ->
                        saveMetadata(capture.captureId, title.getText().toString(), note.getText().toString()))
                .setNegativeButton("取消", null)
                .show();
    }

    private void saveMetadata(String captureId, String title, String note) {
        executor.execute(() -> {
            try {
                repository.updateMetadata(captureId, title, note);
                runOnUiThread(() -> {
                    toast("已保存在手机");
                    loadCaptures();
                });
            } catch (Throwable error) {
                runOnUiThread(() -> toast("保存失败：" + compact(error)));
            }
        });
    }

    private void togglePlayback(TingleCaptureRepository.Capture capture) {
        if (capture.captureId.equals(playingCaptureId)) {
            releasePlayer();
            adapter.notifyDataSetChanged();
            return;
        }
        if (!capture.hasPlayableAudio()) {
            toast("这条录音没有可播放的原音");
            return;
        }
        releasePlayer();
        try {
            MediaPlayer next = new MediaPlayer();
            next.setAudioAttributes(new AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build());
            next.setDataSource(capture.audioFile.getAbsolutePath());
            next.setOnPreparedListener(prepared -> {
                if (player != prepared) {
                    prepared.release();
                    return;
                }
                playingCaptureId = capture.captureId;
                prepared.start();
                adapter.notifyDataSetChanged();
            });
            next.setOnCompletionListener(completed -> {
                if (player == completed) {
                    releasePlayer();
                    adapter.notifyDataSetChanged();
                }
            });
            next.setOnErrorListener((failed, what, extra) -> {
                if (player == failed) {
                    releasePlayer();
                    toast("原音播放失败");
                    adapter.notifyDataSetChanged();
                }
                return true;
            });
            player = next;
            next.prepareAsync();
        } catch (Throwable error) {
            releasePlayer();
            toast("原音播放失败：" + compact(error));
        }
    }

    private void releasePlayer() {
        MediaPlayer current = player;
        player = null;
        playingCaptureId = "";
        if (current == null) {
            return;
        }
        try {
            current.stop();
        } catch (Throwable ignored) {
            // The player may still be preparing.
        }
        try {
            current.release();
        } catch (Throwable ignored) {
            // Best-effort local playback cleanup.
        }
    }

    private void openMacHistory() {
        Intent intent = new Intent(this, MainActivity.class);
        intent.putExtra(MainActivity.EXTRA_REMOTE_FEATURE, MainActivity.REMOTE_TINGLE_HISTORY);
        intent.addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        startActivity(intent);
        finish();
    }

    static String statusText(TingleCaptureRepository.Capture capture, boolean configured) {
        if (TingleCaptureRepository.SYNC_SYNCED.equals(capture.syncState)) {
            return "已同步";
        }
        if (TingleCaptureRepository.SYNC_UPLOADING.equals(capture.syncState)) {
            return "同步中";
        }
        String error = capture.lastError.toLowerCase(Locale.ROOT);
        boolean authFailure = error.contains("401")
                || error.contains("403")
                || error.contains("授权")
                || error.contains("token");
        if ("recording_failed".equals(capture.state)
                || (TingleCaptureRepository.SYNC_FAILED.equals(capture.syncState)
                && !capture.retryable
                && !authFailure)) {
            return "同步失败";
        }
        if (authFailure) {
            return "需重新授权";
        }
        if (!configured) {
            return "待配对";
        }
        return "待同步";
    }

    private String displayTitle(TingleCaptureRepository.Capture capture) {
        return capture.title.isEmpty() ? "未命名录音 · " + displayTime(capture) : capture.title;
    }

    private String displayTime(TingleCaptureRepository.Capture capture) {
        String raw = capture.createdAt.isEmpty() ? capture.updatedAt : capture.createdAt;
        String value = raw.replace('T', ' ');
        return value.length() > 16 ? value.substring(0, 16) : value;
    }

    private String formatDuration(double seconds) {
        int total = Math.max(0, (int) Math.round(seconds));
        return String.format(Locale.CHINA, "%d:%02d", total / 60, total % 60);
    }

    private String compact(Throwable error) {
        String message = error == null ? "" : error.getMessage();
        if (message == null || message.trim().isEmpty()) {
            message = error == null ? "未知错误" : error.getClass().getSimpleName();
        }
        return message.length() > 100 ? message.substring(0, 100) : message;
    }

    private Button toolbarButton(String label) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setTextColor(ClickUi.primaryLabel(this, interfaceDarkMode));
        button.setTextSize(15);
        button.setPadding(0, 0, 0, 0);
        button.setMinWidth(0);
        button.setMinimumWidth(0);
        button.setMinHeight(0);
        button.setMinimumHeight(0);
        button.setBackground(ClickUi.ripple(
                Color.TRANSPARENT,
                dp(24),
                Color.TRANSPARENT,
                0,
                ClickUi.accent(this, interfaceDarkMode)
        ));
        return button;
    }

    private Button toolbarIconButton(int iconResource, String description) {
        Button button = toolbarButton("");
        ClickUi.styleIconButton(button, iconResource, description, interfaceDarkMode);
        return button;
    }

    private TextView text(String value, int sp, int color) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sp);
        view.setTextColor(color);
        return view;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }

    private final class CaptureAdapter extends BaseAdapter {
        private List<TingleCaptureRepository.Capture> captures = Collections.emptyList();
        private boolean configured;

        void replace(List<TingleCaptureRepository.Capture> next, boolean nextConfigured) {
            captures = next;
            configured = nextConfigured;
            notifyDataSetChanged();
        }

        @Override
        public int getCount() {
            return captures.size();
        }

        @Override
        public TingleCaptureRepository.Capture getItem(int position) {
            return captures.get(position);
        }

        @Override
        public long getItemId(int position) {
            return getItem(position).captureId.hashCode();
        }

        @Override
        public View getView(int position, View convertView, ViewGroup parent) {
            LinearLayout row;
            TextView title;
            TextView details;
            TextView note;
            if (convertView instanceof LinearLayout && convertView.getTag() instanceof View[]) {
                row = (LinearLayout) convertView;
                View[] views = (View[]) row.getTag();
                title = (TextView) views[0];
                details = (TextView) views[1];
                note = (TextView) views[2];
            } else {
                row = new LinearLayout(TingleLocalActivity.this);
                row.setOrientation(LinearLayout.VERTICAL);
                row.setPadding(dp(16), dp(13), dp(16), dp(13));
                row.setBackgroundColor(ClickUi.canvas(TingleLocalActivity.this, interfaceDarkMode));
                title = text("", 17, ClickUi.primaryLabel(TingleLocalActivity.this, interfaceDarkMode));
                title.setTypeface(Typeface.DEFAULT_BOLD);
                title.setSingleLine(true);
                title.setEllipsize(android.text.TextUtils.TruncateAt.END);
                row.addView(title);
                details = text("", 13, ClickUi.secondaryLabel(TingleLocalActivity.this, interfaceDarkMode));
                details.setPadding(0, dp(5), 0, 0);
                row.addView(details);
                note = text("", 13, ClickUi.secondaryLabel(TingleLocalActivity.this, interfaceDarkMode));
                note.setPadding(0, dp(5), 0, 0);
                note.setMaxLines(2);
                note.setEllipsize(android.text.TextUtils.TruncateAt.END);
                row.addView(note);
                row.setTag(new View[]{title, details, note});
            }

            TingleCaptureRepository.Capture capture = getItem(position);
            title.setText(
                    (capture.captureId.equals(playingCaptureId) ? "▶ " : "")
                            + displayTitle(capture)
            );
            String status = statusText(capture, configured);
            details.setText(
                    status + " · " + formatDuration(capture.durationSeconds)
                            + " · " + displayTime(capture)
            );
            details.setTextColor(statusColor(status));
            note.setText(capture.note.isEmpty() ? "点开可播放或编辑" : capture.note);
            return row;
        }

        private int statusColor(String status) {
            if ("已同步".equals(status)) {
                return ClickUi.accent(TingleLocalActivity.this, interfaceDarkMode);
            }
            if ("需重新授权".equals(status) || "同步失败".equals(status)) {
                return ClickUi.destructive(TingleLocalActivity.this, interfaceDarkMode);
            }
            if ("同步中".equals(status)) {
                return ClickUi.accent(TingleLocalActivity.this, interfaceDarkMode);
            }
            return ClickUi.warning(TingleLocalActivity.this, interfaceDarkMode);
        }
    }
}
