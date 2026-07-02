package com.click.shell;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.util.Base64;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.webkit.JavascriptInterface;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.AdapterView;
import android.widget.ArrayAdapter;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.PopupWindow;
import android.widget.ProgressBar;
import android.widget.Spinner;
import android.widget.TextView;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.PrintWriter;
import java.io.RandomAccessFile;
import java.io.StringWriter;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import android.window.OnBackInvokedCallback;
import android.window.OnBackInvokedDispatcher;

import org.json.JSONObject;

public final class MainActivity extends Activity {
    private static final String PREFS = "ClickShellPrefs";
    private static final String KEY_HOST = "host";
    private static final String KEY_PORT = "port";
    private static final String KEY_HERMES_PORT = "hermes_port";
    private static final String KEY_RECENT_HOSTS = "recent_hosts";
    private static final String KEY_DEVICE_ID = "device_id";
    private static final String KEY_ACCESS_TOKEN = "access_token";
    private static final String DEFAULT_PORT = "18180";
    private static final String DEFAULT_HERMES_PORT = "8765";
    private static final String DEFAULT_HOST = BuildConfig.CLICK_DEFAULT_HOST;
    private static final int MAX_RECENT_HOSTS = 6;
    private static final int REQUEST_FILE_CHOOSER = 1002;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private SharedPreferences prefs;
    private FrameLayout root;
    private WebView webView;
    private String baseUrl;
    private PermissionRequest pendingAudioPermissionRequest;
    private ValueCallback<Uri[]> pendingFilePathCallback;
    private OnBackInvokedCallback backInvokedCallback;
    private AudioRecord nativeAudioRecord;
    private Thread nativeAudioThread;
    private volatile boolean nativeAudioRecording;
    private File nativeAudioFile;
    private long nativeAudioStartedAt;
    private boolean pendingNativeAudioStart;
    private String nativeAudioMode = "recording";
    private String nativeAudioReaderBookId = "";
    private float edgeSwipeStartX;
    private float edgeSwipeStartY;
    private long edgeSwipeStartAt;
    private boolean edgeSwipeCandidate;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        try {
            startShell();
            registerBackCallback();
        } catch (Throwable throwable) {
            showFatalError(throwable);
        }
    }

    @Override
    protected void onDestroy() {
        executor.shutdownNow();
        releaseNativeAudioRecorder();
        if (webView != null) {
            try {
                webView.destroy();
            } catch (Throwable ignored) {
                // Keep shutdown best-effort; app exit should not crash.
            }
        }
        unregisterBackCallback();
        super.onDestroy();
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] grantResults) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);
        if (requestCode != 1001) {
            return;
        }
        boolean granted = grantResults.length > 0 && grantResults[0] == PackageManager.PERMISSION_GRANTED;
        if (pendingAudioPermissionRequest != null) {
            PermissionRequest request = pendingAudioPermissionRequest;
            pendingAudioPermissionRequest = null;
            try {
                if (granted) {
                    request.grant(request.getResources());
                } else {
                    request.deny();
                }
            } catch (Throwable ignored) {
                // The WebView permission request may expire while Android shows the dialog.
            }
        }
        if (pendingNativeAudioStart) {
            pendingNativeAudioStart = false;
            if (granted) {
                startNativeAudioRecorder();
            } else {
                notifyNativeAudioError("麦克风权限未授权");
            }
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQUEST_FILE_CHOOSER || pendingFilePathCallback == null) {
            return;
        }
        ValueCallback<Uri[]> callback = pendingFilePathCallback;
        pendingFilePathCallback = null;
        Uri[] results = null;
        if (resultCode == RESULT_OK) {
            results = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
        }
        callback.onReceiveValue(results);
    }

    private void startShell() {
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        hideSystemChrome();

        prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(5, 5, 5));
        setContentView(root);

        String savedHost = prefs.getString(KEY_HOST, "");
        String savedPort = prefs.getString(KEY_PORT, DEFAULT_PORT);
        String savedHermesPort = prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT);
        ensureDeviceId();
        if (savedHost != null && !savedHost.trim().isEmpty()) {
            showConnectionView(savedHost, savedPort, savedHermesPort, accessToken(), "正在尝试上次连接...");
            connect(savedHost, savedPort, savedHermesPort, accessToken());
        } else if (!defaultHost().isEmpty()) {
            showConnectionView(defaultHost(), DEFAULT_PORT, savedHermesPort, accessToken(), "正在连接默认 Mac，可在右上角菜单里更换地址。");
            connect(defaultHost(), DEFAULT_PORT, savedHermesPort, accessToken());
        } else {
            showConnectionView("", DEFAULT_PORT, savedHermesPort, accessToken(), "");
        }
    }

    private void hideSystemChrome() {
        Window window = getWindow();
        window.setStatusBarColor(Color.TRANSPARENT);
        window.setNavigationBarColor(Color.BLACK);
        window.getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
        );
    }

    private void showFatalError(Throwable throwable) {
        FrameLayout fallback = new FrameLayout(this);
        fallback.setBackgroundColor(Color.rgb(5, 5, 5));

        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setPadding(dpSafe(24), dpSafe(24), dpSafe(24), dpSafe(24));
        panel.setBackgroundColor(Color.rgb(7, 8, 6));

        TextView title = new TextView(this);
        title.setText("本地工作台启动失败");
        title.setTextColor(Color.WHITE);
        title.setTextSize(24);
        title.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        panel.addView(title, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        TextView body = new TextView(this);
        body.setText("这版已经拦住了闪退。请把下面这行发给我，我继续修：\n\n" + compactError(throwable));
        body.setTextColor(Color.rgb(184, 184, 164));
        body.setTextSize(15);
        body.setPadding(0, dpSafe(16), 0, 0);
        panel.addView(body, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));

        fallback.addView(panel, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER
        ));
        setContentView(fallback);
    }

    private String compactError(Throwable throwable) {
        StringWriter writer = new StringWriter();
        throwable.printStackTrace(new PrintWriter(writer));
        String stack = writer.toString().trim();
        if (stack.length() > 900) {
            return stack.substring(0, 900);
        }
        return stack;
    }

    private void showConnectionView(String hostValue, String portValue, String hermesPortValue, String tokenValue, String messageValue) {
        root.removeAllViews();

        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setGravity(Gravity.CENTER_HORIZONTAL);
        panel.setPadding(dp(28), dp(28), dp(28), dp(28));
        panel.setBackgroundColor(Color.rgb(7, 8, 6));

        TextView title = text("本地工作台", 38, Color.WHITE);
        title.setGravity(Gravity.START);
        title.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        panel.addView(title, matchWrap());

        TextView subtitle = text("连接你的 Mac", 20, Color.rgb(184, 184, 164));
        subtitle.setPadding(0, dp(6), 0, dp(24));
        panel.addView(subtitle, matchWrap());

        EditText host = field("<mac-lan-ip>");
        String displayHost = (hostValue == null || hostValue.trim().isEmpty()) ? defaultHost() : hostValue;
        host.setText(displayHost);
        panel.addView(label("Mac 地址"));
        panel.addView(host, matchFixedHeight(54));

        EditText port = field(DEFAULT_PORT);
        port.setText((portValue == null || portValue.trim().isEmpty()) ? DEFAULT_PORT : portValue);
        port.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);

        List<String> recentHosts = recentHostOptions(displayHost, portValue);
        panel.addView(label("最近访问过的 Mac 地址"));
        Spinner recentHostSpinner = new Spinner(this);
        ArrayAdapter<String> recentAdapter = new ArrayAdapter<>(this, android.R.layout.simple_spinner_item, recentHosts);
        recentAdapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        recentHostSpinner.setAdapter(recentAdapter);
        recentHostSpinner.setOnItemSelectedListener(new AdapterView.OnItemSelectedListener() {
            @Override
            public void onItemSelected(AdapterView<?> parent, View view, int position, long id) {
                String selected = recentHosts.get(position);
                if (position <= 0 || selected.trim().isEmpty()) {
                    return;
                }
                Uri uri = Uri.parse(selected.contains("://") ? selected : "http://" + selected);
                String selectedHost = uri.getHost();
                int selectedPort = uri.getPort();
                if (selectedHost != null && !selectedHost.trim().isEmpty()) {
                    host.setText(selectedHost);
                    if (selectedPort > 0) {
                        port.setText(String.valueOf(selectedPort));
                    }
                }
            }

            @Override
            public void onNothingSelected(AdapterView<?> parent) {
                // No-op. Manual input remains the source of truth.
            }
        });
        panel.addView(recentHostSpinner, matchFixedHeight(54));

        panel.addView(label("本地服务端口"));
        panel.addView(port, matchFixedHeight(54));

        EditText hermesPort = field(DEFAULT_HERMES_PORT);
        hermesPort.setText((hermesPortValue == null || hermesPortValue.trim().isEmpty()) ? DEFAULT_HERMES_PORT : hermesPortValue);
        hermesPort.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
        panel.addView(label("Hermes 端口"));
        panel.addView(hermesPort, matchFixedHeight(54));

        TextView device = text("设备 ID：" + ensureDeviceId(), 12, Color.rgb(130, 130, 118));
        device.setPadding(0, dp(8), 0, 0);
        panel.addView(device, matchWrap());

        Button connect = new Button(this);
        connect.setText("连接");
        connect.setAllCaps(false);
        connect.setOnClickListener(v -> connect(host.getText().toString(), port.getText().toString(), hermesPort.getText().toString(), tokenValue == null ? "" : tokenValue));
        LinearLayout.LayoutParams buttonParams = matchFixedHeight(52);
        buttonParams.setMargins(0, dp(20), 0, dp(12));
        panel.addView(connect, buttonParams);

        TextView message = text(messageValue == null ? "" : messageValue, 15, Color.rgb(184, 184, 164));
        message.setId(View.generateViewId());
        panel.addView(message, matchWrap());

        FrameLayout.LayoutParams panelParams = new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT,
                Gravity.CENTER
        );
        panelParams.setMargins(dp(24), 0, dp(24), 0);
        root.addView(panel, panelParams);
    }

    private void connect(String hostInput, String portInput, String hermesPortInput, String tokenInput) {
        String normalized = normalizeBaseUrl(hostInput, portInput);
        if (normalized == null) {
            showConnectionView(defaultHost(), portInput, hermesPortInput, tokenInput, "请输入 Mac 的局域网地址。");
            return;
        }
        showCheckingOverlay();
        executor.execute(() -> {
            boolean ok = checkHealth(normalized + "/health");
            runOnUiThread(() -> {
                if (ok) {
                    baseUrl = normalized;
                    prefs.edit()
                            .putString(KEY_HOST, hostInput.trim())
                            .putString(KEY_PORT, normalizePort(portInput))
                            .putString(KEY_HERMES_PORT, normalizePortWithDefault(hermesPortInput, DEFAULT_HERMES_PORT))
                            .putString(KEY_ACCESS_TOKEN, tokenInput == null ? "" : tokenInput.trim())
                            .apply();
                    rememberRecentHost(normalized);
                    showWebView(withAccessParams(normalized + "/home"));
                } else {
                    showConnectionView(hostInput, portInput, hermesPortInput, tokenInput, "没有连上本地工作台。请确认 Mac 已打开、同一 Wi-Fi、18180 正在运行，或在这里改 Mac 地址。");
                }
            });
        });
    }

    private void showCheckingOverlay() {
        ProgressBar progress = new ProgressBar(this);
        FrameLayout.LayoutParams params = new FrameLayout.LayoutParams(dp(44), dp(44), Gravity.CENTER);
        root.addView(progress, params);
    }

    private boolean checkHealth(String healthUrl) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(healthUrl).openConnection();
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(5000);
            connection.setRequestMethod("GET");
            int code = connection.getResponseCode();
            return code >= 200 && code < 300;
        } catch (IOException ignored) {
            return false;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private void showWebView(String url) {
        root.removeAllViews();

        try {
            webView = new WebView(this);
        } catch (Throwable throwable) {
                showConnectionView(
                        prefs.getString(KEY_HOST, ""),
                        prefs.getString(KEY_PORT, DEFAULT_PORT),
                        prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT),
                        accessToken(),
                        "系统 WebView 启动失败。请确认 Android System WebView 或 Chrome 已启用。"
                );
            return;
        }
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setMediaPlaybackRequiresUserGesture(false);
        settings.setLoadWithOverviewMode(true);
        settings.setUseWideViewPort(true);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);
        webView.addJavascriptInterface(new NativeAudioBridge(), "ClickNativeAudio");

        webView.setBackgroundColor(Color.BLACK);
        webView.setOnTouchListener((view, event) -> {
            if (event == null) {
                return false;
            }
            switch (event.getActionMasked()) {
                case MotionEvent.ACTION_DOWN:
                    edgeSwipeStartX = event.getX();
                    edgeSwipeStartY = event.getY();
                    edgeSwipeStartAt = System.currentTimeMillis();
                    edgeSwipeCandidate = edgeSwipeStartX <= dp(28) || edgeSwipeStartX >= Math.max(0, view.getWidth() - dp(28));
                    break;
                case MotionEvent.ACTION_UP:
                    if (edgeSwipeCandidate) {
                        float deltaX = event.getX() - edgeSwipeStartX;
                        float deltaY = event.getY() - edgeSwipeStartY;
                        long elapsed = System.currentTimeMillis() - edgeSwipeStartAt;
                        if (Math.abs(deltaX) > dp(72) && Math.abs(deltaX) > Math.abs(deltaY) * 1.6f && elapsed < 900) {
                            handleBackNavigation();
                            edgeSwipeCandidate = false;
                            return true;
                        }
                    }
                    edgeSwipeCandidate = false;
                    break;
                case MotionEvent.ACTION_CANCEL:
                    edgeSwipeCandidate = false;
                    break;
                default:
                    break;
            }
            return false;
        });
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request != null && request.isForMainFrame()) {
                    showConnectionView(
                            prefs.getString(KEY_HOST, ""),
                            prefs.getString(KEY_PORT, DEFAULT_PORT),
                            prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT),
                            accessToken(),
                            "页面加载失败，请检查 Mac 服务和网络。"
                    );
                }
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                if (android.os.Build.VERSION.SDK_INT >= 23
                        && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    pendingAudioPermissionRequest = request;
                    requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1001);
                    return;
                }
                try {
                    request.grant(request.getResources());
                } catch (Throwable ignored) {
                    request.deny();
                }
            }

            @Override
            public boolean onShowFileChooser(
                    WebView view,
                    ValueCallback<Uri[]> filePathCallback,
                    WebChromeClient.FileChooserParams fileChooserParams
            ) {
                if (pendingFilePathCallback != null) {
                    pendingFilePathCallback.onReceiveValue(null);
                }
                pendingFilePathCallback = filePathCallback;
                Intent intent;
                try {
                    intent = fileChooserParams.createIntent();
                } catch (Throwable ignored) {
                    intent = new Intent(Intent.ACTION_GET_CONTENT);
                    intent.addCategory(Intent.CATEGORY_OPENABLE);
                    intent.setType("audio/*");
                }
                try {
                    startActivityForResult(Intent.createChooser(intent, "选择或录制音频"), REQUEST_FILE_CHOOSER);
                    return true;
                } catch (Throwable ignored) {
                    pendingFilePathCallback = null;
                    filePathCallback.onReceiveValue(null);
                    return false;
                }
            }
        });

        root.addView(webView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));

        webView.loadUrl(url);
    }

    private final class NativeAudioBridge {
        @JavascriptInterface
        public boolean isAvailable() {
            return true;
        }

        @JavascriptInterface
        public void startRecording() {
            runOnUiThread(() -> {
                nativeAudioMode = "recording";
                nativeAudioReaderBookId = "";
                requestOrStartNativeAudio();
            });
        }

        @JavascriptInterface
        public void startReaderNote(String bookId) {
            runOnUiThread(() -> {
                nativeAudioMode = "reader_note";
                nativeAudioReaderBookId = bookId == null ? "" : bookId;
                requestOrStartNativeAudio();
            });
        }

        @JavascriptInterface
        public void stopRecording() {
            runOnUiThread(() -> stopNativeAudioRecording());
        }
    }

    private void requestOrStartNativeAudio() {
        if (baseUrl == null || baseUrl.trim().isEmpty()) {
            notifyNativeAudioError("尚未连接 Mac");
            return;
        }
        if (Build.VERSION.SDK_INT >= 23
                && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
            pendingNativeAudioStart = true;
            requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1001);
            return;
        }
        startNativeAudioRecorder();
    }

    private void startNativeAudioRecorder() {
        if (nativeAudioRecord != null || nativeAudioRecording) {
            notifyNativeAudioStarted();
            return;
        }
        try {
            File directory = new File(getCacheDir(), "native-recordings");
            if (!directory.exists() && !directory.mkdirs()) {
                throw new IOException("cannot create native recording directory");
            }
            File audioFile = new File(directory, "click-native-" + UUID.randomUUID() + ".wav");
            nativeAudioFile = audioFile;
            int sampleRate = 16000;
            int channelConfig = AudioFormat.CHANNEL_IN_MONO;
            int audioFormat = AudioFormat.ENCODING_PCM_16BIT;
            int minBuffer = AudioRecord.getMinBufferSize(sampleRate, channelConfig, audioFormat);
            if (minBuffer <= 0) {
                throw new IOException("cannot create native wav recorder buffer");
            }
            int bufferSize = Math.max(minBuffer, sampleRate);
            AudioRecord recorder = new AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    sampleRate,
                    channelConfig,
                    audioFormat,
                    bufferSize
            );
            if (recorder.getState() != AudioRecord.STATE_INITIALIZED) {
                recorder.release();
                throw new IOException("native wav recorder is not initialized");
            }
            nativeAudioRecord = recorder;
            nativeAudioRecording = true;
            nativeAudioStartedAt = System.currentTimeMillis();
            Thread thread = new Thread(
                    () -> writeNativeWavRecording(recorder, audioFile, sampleRate, bufferSize),
                    "click-native-wav-recorder"
            );
            nativeAudioThread = thread;
            recorder.startRecording();
            thread.start();
            notifyNativeAudioStarted();
        } catch (Throwable throwable) {
            releaseNativeAudioRecorder();
            notifyNativeAudioError("原生录音启动失败：" + compactError(throwable));
        }
    }

    private void stopNativeAudioRecording() {
        AudioRecord recorder = nativeAudioRecord;
        File audioFile = nativeAudioFile;
        Thread audioThread = nativeAudioThread;
        long startedAt = nativeAudioStartedAt;
        String uploadMode = nativeAudioMode;
        String readerBookId = nativeAudioReaderBookId;
        nativeAudioRecord = null;
        nativeAudioFile = null;
        nativeAudioThread = null;
        nativeAudioRecording = false;
        nativeAudioStartedAt = 0L;
        nativeAudioMode = "recording";
        nativeAudioReaderBookId = "";
        if (recorder == null || audioFile == null) {
            notifyNativeAudioError("当前没有正在录音");
            return;
        }
        try {
            if (recorder.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                recorder.stop();
            }
        } catch (Throwable stopError) {
            // Continue to release and inspect the file below.
        }
        if (audioThread != null) {
            try {
                audioThread.join(2000);
            } catch (Throwable ignored) {
                // Best-effort shutdown; the recorder is already stopped.
            }
        }
        try {
            recorder.release();
        } catch (Throwable ignored) {
            // Nothing to do.
        }
        if (!audioFile.exists() || audioFile.length() <= 44L) {
            if (audioFile.exists()) {
                //noinspection ResultOfMethodCallIgnored
                audioFile.delete();
            }
            notifyNativeAudioError("没有录到可保存的音频");
            return;
        }
        double durationSeconds = Math.max(0.1, (System.currentTimeMillis() - startedAt) / 1000.0);
        notifyNativeAudioStopping(uploadMode);
        executor.execute(() -> uploadNativeAudio(audioFile, durationSeconds, uploadMode, readerBookId));
    }

    private void releaseNativeAudioRecorder() {
        AudioRecord recorder = nativeAudioRecord;
        Thread audioThread = nativeAudioThread;
        nativeAudioRecord = null;
        nativeAudioThread = null;
        nativeAudioRecording = false;
        nativeAudioStartedAt = 0L;
        if (recorder != null) {
            try {
                if (recorder.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING) {
                    recorder.stop();
                }
            } catch (Throwable ignored) {
                // Recorder may not have started; release below.
            }
            if (audioThread != null) {
                try {
                    audioThread.join(1000);
                } catch (Throwable ignored) {
                    // Best-effort shutdown only.
                }
            }
            try {
                recorder.release();
            } catch (Throwable ignored) {
                // Nothing to do.
            }
        }
    }

    private void writeNativeWavRecording(AudioRecord recorder, File audioFile, int sampleRate, int bufferSize) {
        long dataLength = 0L;
        byte[] buffer = new byte[Math.max(2048, bufferSize)];
        try (RandomAccessFile output = new RandomAccessFile(audioFile, "rw")) {
            output.setLength(0);
            writeWavHeader(output, sampleRate, 1, 16, 0);
            while (nativeAudioRecording) {
                int read = recorder.read(buffer, 0, buffer.length);
                if (read > 0) {
                    output.write(buffer, 0, read);
                    dataLength += read;
                }
            }
            updateWavHeader(output, dataLength);
        } catch (Throwable throwable) {
            notifyNativeAudioError("原生 WAV 录音失败：" + compactError(throwable));
        }
    }

    private void writeWavHeader(RandomAccessFile output, int sampleRate, int channels, int bitsPerSample, long dataLength) throws IOException {
        long byteRate = (long) sampleRate * channels * bitsPerSample / 8L;
        int blockAlign = channels * bitsPerSample / 8;
        output.writeBytes("RIFF");
        writeLittleEndianInt(output, 36L + dataLength);
        output.writeBytes("WAVE");
        output.writeBytes("fmt ");
        writeLittleEndianInt(output, 16);
        writeLittleEndianShort(output, 1);
        writeLittleEndianShort(output, channels);
        writeLittleEndianInt(output, sampleRate);
        writeLittleEndianInt(output, byteRate);
        writeLittleEndianShort(output, blockAlign);
        writeLittleEndianShort(output, bitsPerSample);
        output.writeBytes("data");
        writeLittleEndianInt(output, dataLength);
    }

    private void updateWavHeader(RandomAccessFile output, long dataLength) throws IOException {
        output.seek(4);
        writeLittleEndianInt(output, 36L + dataLength);
        output.seek(40);
        writeLittleEndianInt(output, dataLength);
    }

    private void writeLittleEndianInt(RandomAccessFile output, long value) throws IOException {
        output.write((int) (value & 0xff));
        output.write((int) ((value >> 8) & 0xff));
        output.write((int) ((value >> 16) & 0xff));
        output.write((int) ((value >> 24) & 0xff));
    }

    private void writeLittleEndianShort(RandomAccessFile output, int value) throws IOException {
        output.write(value & 0xff);
        output.write((value >> 8) & 0xff);
    }

    private void uploadNativeAudio(File audioFile, double durationSeconds, String uploadMode, String readerBookId) {
        try {
            byte[] bytes = readAllBytes(audioFile);
            if (bytes.length == 0) {
                throw new IOException("empty native recording");
            }
            JSONObject payload = new JSONObject();
            payload.put("audio_base64", Base64.encodeToString(bytes, Base64.NO_WRAP));
            payload.put("mime_type", audioFile.getName().endsWith(".wav") ? "audio/wav" : "audio/mp4");
            payload.put("duration_seconds", durationSeconds);
            String callback = "__clickNativeAudioDidUpload";
            String uploadPath = "/v1/recordings";
            if ("reader_note".equals(uploadMode)) {
                if (readerBookId == null || readerBookId.trim().isEmpty()) {
                    throw new IOException("missing reader book id");
                }
                payload.put("book_id", readerBookId.trim());
                callback = "__clickNativeReaderAudioDidUpload";
                uploadPath = "/lan/audio-notes/transcribe";
            } else {
                payload.put("source_app", "Click");
                payload.put("source_feature", "Standalone recording");
                payload.put("device_id", ensureDeviceId());
                payload.put("access_token", accessToken());
            }

            HttpURLConnection connection = (HttpURLConnection) new URL(baseUrl + uploadPath).openConnection();
            connection.setConnectTimeout(10000);
            connection.setReadTimeout(180000);
            connection.setRequestMethod("POST");
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            byte[] body = payload.toString().getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(body.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(body);
            }
            int code = connection.getResponseCode();
            String responseText = readResponseText(connection, code);
            connection.disconnect();
            if (code < 200 || code >= 300) {
                throw new IOException("Mac 保存失败：" + code + " " + responseText);
            }
            JSONObject result = new JSONObject();
            result.put("ok", true);
            result.put("response_text", responseText);
            try {
                result.put("response_json", new JSONObject(responseText));
            } catch (Throwable ignored) {
                // Some endpoints may return a non-object response; response_text remains available.
            }
            postNativeAudioEvent(callback, result);
        } catch (Throwable throwable) {
            if ("reader_note".equals(uploadMode)) {
                notifyNativeReaderAudioError("语音备注上传失败：" + compactError(throwable));
            } else {
                notifyNativeAudioError("原生录音上传失败：" + compactError(throwable));
            }
        } finally {
            if (audioFile.exists()) {
                //noinspection ResultOfMethodCallIgnored
                audioFile.delete();
            }
        }
    }

    private byte[] readAllBytes(File file) throws IOException {
        try (FileInputStream input = new FileInputStream(file);
             ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[8192];
            int read;
            while ((read = input.read(buffer)) >= 0) {
                output.write(buffer, 0, read);
            }
            return output.toByteArray();
        }
    }

    private String readResponseText(HttpURLConnection connection, int code) {
        try {
            java.io.InputStream stream = code >= 200 && code < 300
                    ? connection.getInputStream()
                    : connection.getErrorStream();
            if (stream == null) {
                return "";
            }
            try (java.io.InputStream input = stream;
                 ByteArrayOutputStream output = new ByteArrayOutputStream()) {
                byte[] buffer = new byte[4096];
                int read;
                while ((read = input.read(buffer)) >= 0) {
                    output.write(buffer, 0, read);
                }
                return output.toString("UTF-8");
            }
        } catch (Throwable ignored) {
            return "";
        }
    }

    private void notifyNativeAudioStarted() {
        JSONObject payload = new JSONObject();
        try {
            payload.put("ok", true);
        } catch (Throwable ignored) {
            // Static payload.
        }
        postNativeAudioEvent("__clickNativeAudioDidStart", payload);
        if ("reader_note".equals(nativeAudioMode)) {
            postNativeAudioEvent("__clickNativeReaderAudioDidStart", payload);
        }
    }

    private void notifyNativeAudioStopping(String uploadMode) {
        JSONObject payload = new JSONObject();
        try {
            payload.put("ok", true);
        } catch (Throwable ignored) {
            // Static payload.
        }
        postNativeAudioEvent("__clickNativeAudioDidStop", payload);
        if ("reader_note".equals(uploadMode)) {
            postNativeAudioEvent("__clickNativeReaderAudioDidStop", payload);
        }
    }

    private void notifyNativeAudioError(String message) {
        JSONObject payload = new JSONObject();
        try {
            payload.put("ok", false);
            payload.put("error", message == null ? "录音失败" : message);
        } catch (Throwable ignored) {
            // Static payload.
        }
        postNativeAudioEvent("__clickNativeAudioDidError", payload);
    }

    private void notifyNativeReaderAudioError(String message) {
        JSONObject payload = new JSONObject();
        try {
            payload.put("ok", false);
            payload.put("error", message == null ? "语音备注失败" : message);
        } catch (Throwable ignored) {
            // Static payload.
        }
        postNativeAudioEvent("__clickNativeReaderAudioDidError", payload);
    }

    private void postNativeAudioEvent(String callbackName, JSONObject payload) {
        runOnUiThread(() -> {
            if (webView == null) {
                return;
            }
            String script = "window." + callbackName + " && window." + callbackName + "(" + payload.toString() + ");";
            webView.evaluateJavascript(script, null);
        });
    }

    private void showShellMenu(View anchor) {
        LinearLayout menu = new LinearLayout(this);
        menu.setOrientation(LinearLayout.VERTICAL);
        menu.setPadding(dp(14), dp(12), dp(14), dp(12));
        menu.setBackgroundColor(Color.rgb(18, 18, 18));

        TextView status = text(connectionLabel(), 13, Color.rgb(184, 184, 164));
        menu.addView(status, matchWrap());
        menu.addView(menuButton("首页", () -> webView.loadUrl(withAccessParams(baseUrl + "/home"))));
        menu.addView(menuButton("Click 阅读", () -> webView.loadUrl(withAccessParams(baseUrl + "/library"))));
        menu.addView(menuButton("录音", () -> webView.loadUrl(withAccessParams(baseUrl + "/recordings"))));
        menu.addView(menuButton("Hermes", () -> webView.loadUrl(withAccessParams(baseUrl + "/hermes"))));
        menu.addView(menuButton("刷新", () -> webView.reload()));
        menu.addView(menuButton("更换地址", () -> showConnectionView(
                preferredHost(),
                prefs.getString(KEY_PORT, DEFAULT_PORT),
                prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT),
                accessToken(),
                ""
        )));

        PopupWindow popup = new PopupWindow(menu, dp(210), ViewGroup.LayoutParams.WRAP_CONTENT, true);
        popup.setOutsideTouchable(true);
        popup.showAsDropDown(anchor, -dp(140), 0);
    }

    private Button menuButton(String label, Runnable action) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        button.setOnClickListener(v -> action.run());
        return button;
    }

    private String connectionLabel() {
        if (baseUrl == null) {
            return "未连接";
        }
        Uri uri = Uri.parse(baseUrl);
        return "已连接 " + (uri.getHost() == null ? "Mac" : uri.getHost());
    }

    @Override
    public void onBackPressed() {
        handleBackNavigation();
    }

    private void registerBackCallback() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU || backInvokedCallback != null) {
            return;
        }
        backInvokedCallback = this::handleBackNavigation;
        getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                OnBackInvokedDispatcher.PRIORITY_DEFAULT,
                backInvokedCallback
        );
    }

    private void unregisterBackCallback() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU || backInvokedCallback == null) {
            return;
        }
        getOnBackInvokedDispatcher().unregisterOnBackInvokedCallback(backInvokedCallback);
        backInvokedCallback = null;
    }

    private void handleBackNavigation() {
        if (webView == null) {
            showConnectionView(
                    preferredHost(),
                    prefs.getString(KEY_PORT, DEFAULT_PORT),
                    prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT),
                    accessToken(),
                    ""
            );
            return;
        }
        if (webView.canGoBack()) {
            webView.goBack();
            return;
        }
        String currentUrl = webView.getUrl();
        if (baseUrl != null && (currentUrl == null || !currentUrl.startsWith(baseUrl + "/home"))) {
            webView.loadUrl(withAccessParams(baseUrl + "/home"));
            return;
        }
        // Keep the app open on the workspace home; Android back/edge gesture should not immediately quit.
    }

    private String normalizeBaseUrl(String hostInput, String portInput) {
        if (hostInput == null || hostInput.trim().isEmpty()) {
            return null;
        }
        String raw = hostInput.trim();
        if (!raw.contains("://")) {
            raw = "http://" + raw;
        }
        Uri uri = Uri.parse(raw);
        String host = uri.getHost();
        if (host == null || host.trim().isEmpty()) {
            return null;
        }
        int port = uri.getPort() > 0 ? uri.getPort() : Integer.parseInt(normalizePort(portInput));
        return "http://" + host + ":" + port;
    }

    private List<String> recentHostOptions(String currentHost, String currentPort) {
        List<String> options = new ArrayList<>();
        options.add("选择最近访问过的地址");
        addUniqueHostOption(options, hostPortOption(currentHost, currentPort));
        for (String stored : storedRecentHosts()) {
            addUniqueHostOption(options, stored);
        }
        return options;
    }

    private List<String> storedRecentHosts() {
        List<String> hosts = new ArrayList<>();
        String raw = prefs.getString(KEY_RECENT_HOSTS, "");
        if (raw == null || raw.trim().isEmpty()) {
            return hosts;
        }
        for (String item : raw.split("\\n")) {
            addUniqueHostOption(hosts, item);
        }
        return hosts;
    }

    private void rememberRecentHost(String normalizedBaseUrl) {
        String option = hostPortOption(normalizedBaseUrl, DEFAULT_PORT);
        if (option.isEmpty()) {
            return;
        }
        List<String> hosts = new ArrayList<>();
        addUniqueHostOption(hosts, option);
        for (String stored : storedRecentHosts()) {
            addUniqueHostOption(hosts, stored);
            if (hosts.size() >= MAX_RECENT_HOSTS) {
                break;
            }
        }
        prefs.edit().putString(KEY_RECENT_HOSTS, joinRecentHosts(hosts)).apply();
    }

    private String hostPortOption(String hostInput, String portInput) {
        String normalized = normalizeBaseUrl(hostInput, portInput);
        if (normalized == null) {
            return "";
        }
        Uri uri = Uri.parse(normalized);
        String host = uri.getHost();
        int port = uri.getPort();
        if (host == null || host.trim().isEmpty() || port <= 0) {
            return "";
        }
        return host.trim() + ":" + port;
    }

    private void addUniqueHostOption(List<String> hosts, String option) {
        if (option == null) {
            return;
        }
        String cleaned = option.trim();
        if (cleaned.isEmpty() || hosts.contains(cleaned)) {
            return;
        }
        hosts.add(cleaned);
    }

    private String joinRecentHosts(List<String> hosts) {
        StringBuilder builder = new StringBuilder();
        for (String host : hosts) {
            if (builder.length() > 0) {
                builder.append('\n');
            }
            builder.append(host);
        }
        return builder.toString();
    }

    private String normalizePort(String portInput) {
        return normalizePortWithDefault(portInput, DEFAULT_PORT);
    }

    private String normalizePortWithDefault(String portInput, String fallback) {
        if (portInput == null || portInput.trim().isEmpty()) {
            return fallback;
        }
        try {
            int port = Integer.parseInt(portInput.trim());
            return port > 0 ? String.valueOf(port) : fallback;
        } catch (NumberFormatException ignored) {
            return fallback;
        }
    }

    private String defaultHost() {
        return DEFAULT_HOST == null ? "" : DEFAULT_HOST.trim();
    }

    private String ensureDeviceId() {
        String existing = prefs.getString(KEY_DEVICE_ID, "");
        if (existing != null && !existing.trim().isEmpty()) {
            return existing.trim();
        }
        String created = "android-" + UUID.randomUUID().toString();
        prefs.edit().putString(KEY_DEVICE_ID, created).apply();
        return created;
    }

    private String accessToken() {
        String token = prefs.getString(KEY_ACCESS_TOKEN, "");
        return token == null ? "" : token.trim();
    }

    private String withAccessParams(String url) {
        Uri.Builder builder = Uri.parse(url).buildUpon();
        builder.appendQueryParameter("device_id", ensureDeviceId());
        builder.appendQueryParameter("device_name", "Android 本地工作台");
        String token = accessToken();
        if (!token.isEmpty()) {
            builder.appendQueryParameter("access_token", token);
        }
        return builder.build().toString();
    }

    private String preferredHost() {
        String saved = prefs.getString(KEY_HOST, "");
        if (saved != null && !saved.trim().isEmpty()) {
            return saved;
        }
        return defaultHost();
    }

    private TextView label(String value) {
        TextView view = text(value, 14, Color.rgb(184, 184, 164));
        view.setPadding(0, dp(14), 0, dp(6));
        return view;
    }

    private EditText field(String hint) {
        EditText editText = new EditText(this);
        editText.setHint(hint);
        editText.setSingleLine(true);
        editText.setTextColor(Color.WHITE);
        editText.setHintTextColor(Color.rgb(130, 130, 118));
        editText.setBackgroundColor(Color.rgb(30, 32, 26));
        editText.setPadding(dp(12), 0, dp(12), 0);
        return editText;
    }

    private TextView text(String value, int sp, int color) {
        TextView view = new TextView(this);
        view.setText(value);
        view.setTextSize(sp);
        view.setTextColor(color);
        return view;
    }

    private LinearLayout.LayoutParams matchWrap() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
    }

    private LinearLayout.LayoutParams matchFixedHeight(int heightDp) {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                dp(heightDp)
        );
    }

    private int dp(int value) {
        float density = getResources().getDisplayMetrics().density;
        return Math.round(value * density);
    }

    private int dpSafe(int value) {
        try {
            return dp(value);
        } catch (Throwable ignored) {
            return value;
        }
    }
}
