package com.click.shell;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Bitmap;
import android.graphics.Color;
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
import android.webkit.WebResourceResponse;
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
import android.widget.ScrollView;
import android.widget.Spinner;
import android.widget.TextView;
import android.widget.Toast;

import androidx.core.view.ViewCompat;
import androidx.core.view.WindowInsetsCompat;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import android.window.OnBackInvokedCallback;
import android.window.OnBackInvokedDispatcher;

import org.json.JSONObject;

public final class MainActivity extends Activity {
    static final String EXTRA_REMOTE_FEATURE = "click_remote_feature";
    static final String REMOTE_TINGLE_HISTORY = "tingle_history";
    static final String REMOTE_HERMES = "hermes";
    static final String PREFLIGHT_CONNECTION_IDENTITY_PREFIX =
            "CLICK_PREFLIGHT_CONNECTION";
    static final String PREFLIGHT_AUTH_RECEIPT_PREFIX =
            "CLICK_PREFLIGHT_AUTH";

    private static final String PREFS = ClickSyncConfig.PREFERENCES_NAME;
    private static final String KEY_HOST = ClickSyncConfig.KEY_HOST;
    private static final String KEY_PORT = ClickSyncConfig.KEY_PORT;
    private static final String KEY_HERMES_PORT = "hermes_port";
    private static final String KEY_RECENT_HOSTS = "recent_hosts";
    private static final String KEY_DEVICE_ID = ClickSyncConfig.KEY_DEVICE_ID;
    private static final String KEY_PAIRING_BASE_URL = "pairing_base_url";
    private static final String KEY_PAIRING_CODE = "pairing_code";
    private static final String LEGACY_KEY_ACCESS_TOKEN = "access_token";
    private static final String DEFAULT_PORT = ClickSyncConfig.DEFAULT_PORT;
    private static final String DEFAULT_HERMES_PORT = "8765";
    private static final String DEFAULT_HOST = BuildConfig.CLICK_DEFAULT_HOST;
    private static final int MAX_RECENT_HOSTS = 6;
    private static final int REQUEST_FILE_CHOOSER = 1002;
    private static final int REQUEST_BOOK_IMPORT = 1003;
    private static final String EXTRA_PREFLIGHT_NONCE = "click_preflight_nonce";

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    private SharedPreferences prefs;
    private AndroidCredentialStore credentialStore;
    private FrameLayout root;
    private WebView webView;
    private String baseUrl;
    private String pairingCode = "";
    private String credentialStoreMessage = "";
    private volatile boolean pairingActionInProgress;
    private PermissionRequest pendingAudioPermissionRequest;
    private ValueCallback<Uri[]> pendingFilePathCallback;
    private OnBackInvokedCallback backInvokedCallback;
    private ClickWavRecorder nativeWavRecorder;
    private File nativeAudioFile;
    private String nativeAudioCaptureId = "";
    private boolean pendingNativeAudioStart;
    private String nativeAudioMode = "recording";
    private String nativeAudioReaderBookId = "";
    private float edgeSwipeStartX;
    private float edgeSwipeStartY;
    private long edgeSwipeStartAt;
    private boolean edgeSwipeCandidate;
    private int remoteProbeGeneration;
    private ClickNetworkRecovery networkRecovery;
    private ClickLanDiscovery lanDiscovery;
    private boolean preflightIdentityProbeMode;
    private String preflightAuthReceipt = "";
    private String activePreflightNonce = "";
    private boolean interfaceDarkMode = true;

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
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        if (!dispatchPreflightIdentityProbe(intent)) {
            dispatchRemoteFeature(intent);
        }
    }

    @Override
    protected void onStart() {
        super.onStart();
        if (preflightIdentityProbeMode) {
            return;
        }
        if (lanDiscovery == null) {
            lanDiscovery = new ClickLanDiscovery(
                    this,
                    executor,
                    this::onVerifiedLanOrigin
            );
        }
        if (networkRecovery == null) {
            networkRecovery = new ClickNetworkRecovery(
                    this,
                    executor,
                    lanDiscovery::refresh
            );
        }
        lanDiscovery.start();
        networkRecovery.start();
        ClickSyncScheduler.enqueueMetadata(this);
        executor.execute(() -> BookImportScheduler.enqueuePending(getApplicationContext()));
        recoverAndSchedulePendingTingle();
        ClickAppUpdater.checkOnForeground(this);
    }

    @Override
    protected void onResume() {
        super.onResume();
        ClickAppUpdater.resumePendingInstall(this);
        boolean resumedFromPreflightProbe = preflightIdentityProbeMode;
        if (resumedFromPreflightProbe) {
            preflightIdentityProbeMode = false;
            Intent intent = getIntent();
            if (intent != null) {
                intent.removeExtra(EXTRA_PREFLIGHT_NONCE);
            }
            return;
        }
        boolean preferredDarkMode =
                new ReaderPreferencesRepository(this).load().getTheme() == ReaderTheme.NIGHT;
        if (webView == null
                && preferredDarkMode != interfaceDarkMode) {
            interfaceDarkMode = preferredDarkMode;
            showConnectionView(
                    preferredHost(),
                    prefs.getString(KEY_PORT, DEFAULT_PORT),
                    prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT),
                    ""
            );
        }
    }

    @Override
    protected void onStop() {
        if (networkRecovery != null) {
            networkRecovery.stop();
        }
        if (lanDiscovery != null) {
            lanDiscovery.stop();
        }
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        if (networkRecovery != null) {
            networkRecovery.stop();
        }
        if (lanDiscovery != null) {
            lanDiscovery.stop();
        }
        executor.shutdownNow();
        releaseNativeAudioRecorder();
        denyPendingWebAudioRequest();
        destroyCurrentWebView();
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
                if (granted && isTrustedAudioCaptureRequest(request)) {
                    request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
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
        if (!granted) {
            showMicrophonePermissionRecovery();
        }
    }

    private void showMicrophonePermissionRecovery() {
        if (isFinishing() || (Build.VERSION.SDK_INT >= 17 && isDestroyed())) {
            return;
        }
        new android.app.AlertDialog.Builder(this)
                .setTitle("Click 需要麦克风权限")
                .setMessage("录音尚未开始，也没有创建空记录。请在应用设置中允许麦克风，然后回到 Click 重试。")
                .setPositiveButton("打开应用设置", (dialog, which) -> {
                    Intent intent = new Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
                    intent.setData(Uri.parse("package:" + getPackageName()));
                    startActivity(intent);
                })
                .setNegativeButton("稍后", null)
                .show();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQUEST_BOOK_IMPORT) {
            if (resultCode == RESULT_OK && data != null) {
                openNativeReader(
                        data.getStringExtra(BookImportActivity.EXTRA_LOCAL_BOOK_ID),
                        data.getStringExtra(BookImportActivity.EXTRA_SOURCE_KIND)
                );
            }
            return;
        }
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
        interfaceDarkMode = new ReaderPreferencesRepository(this).load().getTheme() == ReaderTheme.NIGHT;
        boolean preflightRequest = !preflightNonce(getIntent()).isEmpty();
        String savedPairingCode = prefs.getString(KEY_PAIRING_CODE, "");
        pairingCode = (savedPairingCode == null ? "" : savedPairingCode).replaceAll("\\D", "");
        if (preflightRequest) {
            credentialStore = new AndroidCredentialStore(prefs);
        } else {
            initializeCredentialStore();
        }
        root = new FrameLayout(this);
        root.setBackgroundColor(ClickUi.canvas(this, interfaceDarkMode));
        setContentView(root);
        installImeResizeGuard();

        String savedHost = prefs.getString(KEY_HOST, "");
        String savedPort = prefs.getString(KEY_PORT, DEFAULT_PORT);
        String savedHermesPort = prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT);
        if (!preflightRequest) {
            ensureDeviceId();
        }
        String launchHost = savedHost == null || savedHost.trim().isEmpty() ? defaultHost() : savedHost.trim();
        baseUrl = normalizeBaseUrl(launchHost, savedPort);
        if (dispatchPreflightIdentityProbe(getIntent())) {
            return;
        }
        showConnectionView(
                launchHost,
                savedPort,
                savedHermesPort,
                "阅读器已优先打开；返回此页可连接 Mac、Tingle 或 Hermes。"
        );
        if (!dispatchRemoteFeature(getIntent())) {
            openNativeReader();
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

    private void installImeResizeGuard() {
        ViewCompat.setOnApplyWindowInsetsListener(root, (view, insets) -> {
            int imeBottom = insets.isVisible(WindowInsetsCompat.Type.ime())
                    ? insets.getInsets(WindowInsetsCompat.Type.ime()).bottom
                    : 0;
            if (webView != null && webView.getLayoutParams() instanceof FrameLayout.LayoutParams) {
                FrameLayout.LayoutParams params = (FrameLayout.LayoutParams) webView.getLayoutParams();
                if (params.bottomMargin != imeBottom) {
                    params.bottomMargin = imeBottom;
                    webView.setLayoutParams(params);
                }
            }
            return insets;
        });
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

    private String userFacingUploadError(Throwable throwable) {
        Throwable current = throwable;
        while (current != null) {
            if (current instanceof java.net.ConnectException) {
                return "暂时无法连接 Mac 上的 Click Runtime";
            }
            if (current instanceof java.net.SocketTimeoutException) {
                return "连接 Click Runtime 超时";
            }
            current = current.getCause();
        }
        String message = throwable == null ? "暂时无法完成同步" : throwable.getMessage();
        if (message == null || message.trim().isEmpty()) {
            return "暂时无法完成同步";
        }
        message = message.trim().split("\\R", 2)[0];
        return message.length() > 180 ? message.substring(0, 180) : message;
    }

    private void showConnectionView(String hostValue, String portValue, String hermesPortValue, String messageValue) {
        destroyCurrentWebView();
        root.removeAllViews();

        String currentAccessToken = accessToken();
        String currentPairingSecret = pairingSecret();
        boolean paired = !currentAccessToken.isEmpty();
        boolean pairingPending = !currentPairingSecret.isEmpty();

        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setGravity(Gravity.CENTER_HORIZONTAL);
        panel.setPadding(dp(28), dp(28), dp(28), dp(28));
        panel.setBackgroundColor(ClickUi.canvas(this, interfaceDarkMode));

        TextView title = text("连接与同步", 34, ClickUi.primaryLabel(this, interfaceDarkMode));
        title.setGravity(Gravity.START);
        title.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        panel.addView(title, matchWrap());

        TextView subtitle = text(
                paired ? "已配对 · 同一 Wi‑Fi 自动发现" : "首次连接你的 Mac",
                18,
                ClickUi.secondaryLabel(this, interfaceDarkMode)
        );
        subtitle.setPadding(0, dp(6), 0, dp(24));
        panel.addView(subtitle, matchWrap());

        String visibleMessage = messageValue == null ? "" : messageValue.trim();
        if (!visibleMessage.isEmpty()) {
            TextView message = text(
                    visibleMessage,
                    14,
                    ClickUi.primaryLabel(this, interfaceDarkMode)
            );
            message.setPadding(dp(14), dp(12), dp(14), dp(12));
            message.setBackground(ClickUi.rounded(
                    ClickUi.elevated(this, interfaceDarkMode),
                    dp(16),
                    ClickUi.separator(this, interfaceDarkMode),
                    1
            ));
            LinearLayout.LayoutParams messageParams = matchWrap();
            messageParams.setMargins(0, 0, 0, dp(16));
            panel.addView(message, messageParams);
        }

        LinearLayout productEntries = new LinearLayout(this);
        productEntries.setOrientation(LinearLayout.HORIZONTAL);
        Button readingEntry = new Button(this);
        readingEntry.setText("阅读");
        readingEntry.setAllCaps(false);
        ClickUi.stylePill(readingEntry, false, interfaceDarkMode);
        readingEntry.setOnClickListener(view -> openNativeReader());
        productEntries.addView(readingEntry, new LinearLayout.LayoutParams(0, dp(50), 1));
        Button tingleEntry = new Button(this);
        tingleEntry.setText("Tingle");
        tingleEntry.setAllCaps(false);
        ClickUi.stylePill(tingleEntry, false, interfaceDarkMode);
        tingleEntry.setOnClickListener(view -> openLocalTingle());
        LinearLayout.LayoutParams tingleEntryParams = new LinearLayout.LayoutParams(0, dp(50), 1);
        tingleEntryParams.setMargins(dp(8), 0, 0, 0);
        productEntries.addView(tingleEntry, tingleEntryParams);
        Button hermesEntry = new Button(this);
        hermesEntry.setText("Hermes");
        hermesEntry.setAllCaps(false);
        ClickUi.stylePill(hermesEntry, false, interfaceDarkMode);
        hermesEntry.setOnClickListener(view -> openHermesIfAvailable());
        LinearLayout.LayoutParams hermesEntryParams = new LinearLayout.LayoutParams(0, dp(50), 1);
        hermesEntryParams.setMargins(dp(8), 0, 0, 0);
        productEntries.addView(hermesEntry, hermesEntryParams);
        panel.addView(productEntries, matchWrap());

        Button importEntry = menuButton("＋  导入 EPUB / PDF", this::openBookImport);
        LinearLayout.LayoutParams importEntryParams = matchWrap();
        importEntryParams.setMargins(0, dp(8), 0, dp(14));
        panel.addView(importEntry, importEntryParams);

        int pendingCaptureCount = pendingNativeCaptureCount();
        if (pendingCaptureCount > 0) {
            TextView pendingCapture = text(
                    "有 " + pendingCaptureCount + " 条原始录音已安全保存在手机，连接后会自动同步。",
                    15,
                    Color.rgb(236, 170, 93)
            );
            pendingCapture.setPadding(0, 0, 0, dp(20));
            panel.addView(pendingCapture, matchWrap());
        }

        LinearLayout advancedPanel = new LinearLayout(this);
        advancedPanel.setOrientation(LinearLayout.VERTICAL);

        EditText host = field("192.168.x.x（首次配对）");
        String displayHost = (hostValue == null || hostValue.trim().isEmpty()) ? defaultHost() : hostValue;
        host.setText(displayHost);
        advancedPanel.addView(label("Mac 地址"));
        advancedPanel.addView(host, matchFixedHeight(54));
        TextView hostHelp = text(
                "首次配对输入一次当前地址；之后在同一 Wi‑Fi 下，Click 会自动发现 Mac 的新地址。",
                12,
                Color.rgb(130, 130, 118)
        );
        hostHelp.setPadding(0, dp(4), 0, dp(8));
        advancedPanel.addView(hostHelp, matchWrap());

        EditText port = field(DEFAULT_PORT);
        port.setText((portValue == null || portValue.trim().isEmpty()) ? DEFAULT_PORT : portValue);
        port.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);

        List<String> recentHosts = recentHostOptions(displayHost, portValue);
        advancedPanel.addView(label("最近访问过的 Mac 地址"));
        Spinner recentHostSpinner = new Spinner(this);
        ArrayAdapter<String> recentAdapter = new ArrayAdapter<String>(
                this,
                android.R.layout.simple_spinner_item,
                recentHosts
        ) {
            private TextView styleSpinnerText(View view) {
                TextView text = (TextView) view;
                text.setTextColor(ClickUi.primaryLabel(MainActivity.this, interfaceDarkMode));
                text.setBackgroundColor(ClickUi.surface(MainActivity.this, interfaceDarkMode));
                text.setPadding(dp(12), 0, dp(12), 0);
                return text;
            }

            @Override
            public View getView(int position, View convertView, ViewGroup parent) {
                return styleSpinnerText(super.getView(position, convertView, parent));
            }

            @Override
            public View getDropDownView(int position, View convertView, ViewGroup parent) {
                return styleSpinnerText(super.getDropDownView(position, convertView, parent));
            }
        };
        recentAdapter.setDropDownViewResource(android.R.layout.simple_spinner_dropdown_item);
        recentHostSpinner.setAdapter(recentAdapter);
        recentHostSpinner.setBackground(ClickUi.rounded(
                ClickUi.surface(this, interfaceDarkMode),
                dp(14),
                ClickUi.separator(this, interfaceDarkMode),
                dp(1)
        ));
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
        advancedPanel.addView(recentHostSpinner, matchFixedHeight(54));

        advancedPanel.addView(label("本地服务端口"));
        advancedPanel.addView(port, matchFixedHeight(54));

        EditText hermesPort = field(DEFAULT_HERMES_PORT);
        hermesPort.setText((hermesPortValue == null || hermesPortValue.trim().isEmpty()) ? DEFAULT_HERMES_PORT : hermesPortValue);
        hermesPort.setInputType(android.text.InputType.TYPE_CLASS_NUMBER);
        advancedPanel.addView(label("Hermes 端口"));
        advancedPanel.addView(hermesPort, matchFixedHeight(54));

        ClickSyncConfig savedConnection = ClickSyncConfig.load(this);
        String savedOrigin = savedConnection.baseUrl.isEmpty()
                ? "UNCONFIGURED"
                : savedConnection.baseUrl;
        String endpointMode = "UNCONFIGURED".equals(savedOrigin)
                ? "尚未配置"
                : savedConnection.isReady()
                        ? "同一 Wi‑Fi 自动发现 · 地址变化会自动更新"
                        : "首次配对需手动连接";
        String acceptanceDeviceId = ensureDeviceId();
        TextView device = text(
                "当前生效连接："
                        + ("UNCONFIGURED".equals(savedOrigin) ? "尚未保存" : savedOrigin)
                        + "\n连接方式："
                        + endpointMode
                        + "\n验收设备 ID："
                        + acceptanceDeviceId,
                12,
                Color.rgb(130, 130, 118)
        );
        device.setContentDescription(connectionIdentityMarker(savedOrigin, acceptanceDeviceId));
        device.setClickable(false);
        device.setLongClickable(false);
        device.setFocusable(false);
        device.setTextIsSelectable(false);
        device.setPadding(0, dp(8), 0, 0);
        advancedPanel.addView(device, matchWrap());

        String pairingMessage;
        if (!pairingCode.isEmpty() && pairingPending) {
            pairingMessage = "配对码：" + formatPairingCode(pairingCode)
                    + "\n请在 Mac 上核对并批准，然后点“检查批准”。应用不会在后台反复查询。";
        } else if (pairingPending) {
            pairingMessage = "配对申请已发送，正在等待 Mac 批准。点“检查批准”手动确认；应用不会后台轮询。";
        } else if (paired) {
            pairingMessage = "这台手机已安全配对；在同一 Wi‑Fi 下会自动发现 Mac，访问凭据保存在 Android 系统安全区。";
        } else {
            pairingMessage = "这台手机还没获得 Mac 授权。先点“申请配对”，再在 Mac 上确认六位配对码。";
        }
        if (!credentialStoreMessage.isEmpty()) {
            pairingMessage += "\n" + credentialStoreMessage;
        }
        TextView pairingStatus = text(
                pairingMessage,
                14,
                paired ? Color.rgb(137, 205, 147) : Color.rgb(236, 170, 93)
        );
        pairingStatus.setTextColor(
                paired
                        ? ClickUi.accent(this, interfaceDarkMode)
                        : ClickUi.warning(this, interfaceDarkMode)
        );
        pairingStatus.setPadding(0, dp(14), 0, dp(6));
        panel.addView(pairingStatus, matchWrap());

        Button advancedToggle = menuButton(
                paired ? "高级连接设置" : "收起连接详情",
                () -> {
                    boolean show = advancedPanel.getVisibility() != View.VISIBLE;
                    advancedPanel.setVisibility(show ? View.VISIBLE : View.GONE);
                }
        );
        advancedToggle.setOnClickListener(v -> {
            boolean show = advancedPanel.getVisibility() != View.VISIBLE;
            advancedPanel.setVisibility(show ? View.VISIBLE : View.GONE);
            advancedToggle.setText(show ? "收起连接详情" : "高级连接设置");
        });
        ClickUi.styleSecondaryButton(advancedToggle, interfaceDarkMode);
        panel.addView(advancedToggle, matchFixedHeight(48));
        advancedPanel.setVisibility(paired ? View.GONE : View.VISIBLE);
        panel.addView(advancedPanel, matchWrap());

        if (!paired || pairingPending) {
            LinearLayout pairingActions = new LinearLayout(this);
            pairingActions.setOrientation(LinearLayout.HORIZONTAL);
            Button requestPairing = new Button(this);
            requestPairing.setText("申请配对");
            requestPairing.setAllCaps(false);
            ClickUi.stylePrimary(requestPairing, interfaceDarkMode);
            requestPairing.setOnClickListener(v -> requestPairing(
                    host.getText().toString(),
                    port.getText().toString(),
                    hermesPort.getText().toString()
            ));
            pairingActions.addView(requestPairing, new LinearLayout.LayoutParams(0, dp(50), 1));

            Button checkPairing = new Button(this);
            checkPairing.setText("检查批准");
            checkPairing.setAllCaps(false);
            ClickUi.styleSecondaryButton(checkPairing, interfaceDarkMode);
            checkPairing.setEnabled(!currentPairingSecret.isEmpty());
            checkPairing.setOnClickListener(v -> claimPairing(
                    host.getText().toString(),
                    port.getText().toString(),
                    hermesPort.getText().toString()
            ));
            LinearLayout.LayoutParams checkParams = new LinearLayout.LayoutParams(0, dp(50), 1);
            checkParams.setMargins(dp(8), 0, 0, 0);
            pairingActions.addView(checkPairing, checkParams);
            panel.addView(pairingActions, matchWrap());
        } else {
            Button reauthorize = new Button(this);
            reauthorize.setText("重新配对");
            reauthorize.setAllCaps(false);
            ClickUi.styleSecondaryButton(reauthorize, interfaceDarkMode);
            reauthorize.setOnClickListener(v -> requestPairing(
                    host.getText().toString(),
                    port.getText().toString(),
                    hermesPort.getText().toString()
            ));
            advancedPanel.addView(reauthorize, matchFixedHeight(50));
        }

        Button connect = new Button(this);
        connect.setText(paired ? "检查连接" : "连接");
        connect.setAllCaps(false);
        ClickUi.stylePrimary(connect, interfaceDarkMode);
        connect.setOnClickListener(v -> connect(
                host.getText().toString(),
                port.getText().toString(),
                hermesPort.getText().toString()
        ));
        LinearLayout.LayoutParams buttonParams = matchFixedHeight(52);
        buttonParams.setMargins(0, dp(20), 0, dp(12));
        panel.addView(connect, buttonParams);

        ScrollView scroller = new ScrollView(this);
        scroller.setFillViewport(true);
        scroller.setPadding(dp(24), 0, dp(24), 0);
        scroller.setClipToPadding(false);
        scroller.addView(panel, new ScrollView.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        ));
        root.addView(scroller, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));
    }

    private void connect(String hostInput, String portInput, String hermesPortInput) {
        String normalized = normalizeBaseUrl(hostInput, portInput);
        if (normalized == null) {
            showConnectionView(defaultHost(), portInput, hermesPortInput, "请输入有效的 Mac 地址。");
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
                            .apply();
                    rememberRecentHost(normalized);
                    showWebView(normalized + "/home");
                    syncPendingNativeAudio();
                } else {
                    showConnectionView(hostInput, portInput, hermesPortInput, "没有连上 Mac。请确认手机和 Mac 处于同一 Wi‑Fi；已配对设备会在网络切换后自动重新发现。");
                }
            });
        });
    }

    private void requestPairing(String hostInput, String portInput, String hermesPortInput) {
        if (pairingActionInProgress) {
            return;
        }
        String normalized = normalizeBaseUrl(hostInput, portInput);
        if (normalized == null) {
            showConnectionView(defaultHost(), portInput, hermesPortInput, "请输入有效的 Mac 地址，再申请配对。");
            return;
        }
        rememberConnectionInputs(normalized, hostInput, portInput, hermesPortInput);
        pairingActionInProgress = true;
        showCheckingOverlay();
        executor.execute(() -> {
            try {
                JSONObject body = new JSONObject();
                body.put("device_id", ensureDeviceId());
                body.put("device_name", "Click Android");
                body.put("platform", "android");
                JSONObject response = postPairingJson(normalized + "/v1/mobile/access/request", body);
                String secret = response.optString("pairing_secret", "").trim();
                String code = response.optString("pairing_code", "").trim();
                if (!response.optBoolean("pending", false)
                        || !secret.matches("[0-9a-fA-F]{64}")
                        || !code.matches("\\d{6}")) {
                    throw new IOException("Mac 没有返回有效的六位配对码");
                }
                if (credentialStore == null) {
                    throw new IOException("Android 系统安全区当前不可用");
                }
                credentialStore.savePairingSecret(secret);
                if (!secret.equals(credentialStore.pairingSecret())) {
                    throw new IOException("配对凭据未能安全保存");
                }
                prefs.edit()
                        .putString(KEY_PAIRING_BASE_URL, normalized)
                        .putString(KEY_PAIRING_CODE, code)
                        .apply();
                pairingCode = code;
                credentialStoreMessage = "";
                pairingActionInProgress = false;
                runOnUiThread(() -> showConnectionView(
                        hostInput,
                        portInput,
                        hermesPortInput,
                        "配对申请已发送。请在 Mac 上核对六位配对码。"
                ));
            } catch (Throwable throwable) {
                String error = pairingError("配对申请失败", throwable);
                pairingActionInProgress = false;
                runOnUiThread(() -> showConnectionView(hostInput, portInput, hermesPortInput, error));
            }
        });
    }

    private void claimPairing(String hostInput, String portInput, String hermesPortInput) {
        if (pairingActionInProgress) {
            return;
        }
        String normalized = normalizeBaseUrl(hostInput, portInput);
        if (normalized == null) {
            showConnectionView(defaultHost(), portInput, hermesPortInput, "请输入有效的 Mac 地址，再检查批准状态。");
            return;
        }
        String secret = pairingSecret();
        if (secret.isEmpty()) {
            showConnectionView(hostInput, portInput, hermesPortInput, "没有待检查的配对申请，请先点“申请配对”。");
            return;
        }
        String requestedBaseUrl = prefs.getString(KEY_PAIRING_BASE_URL, "");
        if (requestedBaseUrl != null
                && !requestedBaseUrl.trim().isEmpty()
                && !normalized.equals(requestedBaseUrl.trim())) {
            showConnectionView(
                    hostInput,
                    portInput,
                    hermesPortInput,
                    "这次配对申请属于另一个 Mac 地址。请切回原地址检查，或在当前地址重新申请。"
            );
            return;
        }
        rememberConnectionInputs(normalized, hostInput, portInput, hermesPortInput);
        pairingActionInProgress = true;
        showCheckingOverlay();
        executor.execute(() -> {
            try {
                JSONObject body = new JSONObject();
                body.put("device_id", ensureDeviceId());
                body.put("pairing_secret", secret);
                JSONObject response = postPairingJson(normalized + "/v1/mobile/access/claim", body);
                if (response.optBoolean("authorized", false)) {
                    String token = response.optString("access_token", "").trim();
                    if (token.isEmpty()) {
                        throw new IOException("Mac 已批准，但没有返回访问凭据");
                    }
                    if (credentialStore == null) {
                        throw new IOException("Android 系统安全区当前不可用");
                    }
                    credentialStore.saveAccessToken(token);
                    if (!token.equals(credentialStore.accessToken())) {
                        throw new IOException("访问凭据未能安全保存");
                    }
                    credentialStore.clearPairingSecret();
                    prefs.edit()
                            .remove(KEY_PAIRING_BASE_URL)
                            .remove(KEY_PAIRING_CODE)
                            .apply();
                    String completionMessage = "配对完成。以后在同一 Wi‑Fi 下，Click 会自动发现这台 Mac 的当前地址。";
                    pairingCode = "";
                    credentialStoreMessage = "";
                    pairingActionInProgress = false;
                    ClickSyncScheduler.enqueueMetadata(this);
                    BookImportScheduler.enqueuePending(this);
                    TingleUploadScheduler.enqueue(this);
                    runOnUiThread(() -> showConnectionView(
                            hostInput,
                            portInput,
                            hermesPortInput,
                            completionMessage
                    ));
                    return;
                }
                if (response.optBoolean("pending", false)) {
                    pairingActionInProgress = false;
                    runOnUiThread(() -> showConnectionView(
                            hostInput,
                            portInput,
                            hermesPortInput,
                            "Mac 还没有批准。请核对配对码，批准后再点一次“检查批准”；应用不会自动轮询。"
                    ));
                    return;
                }
                throw new IOException("这次配对申请已失效，请重新申请");
            } catch (Throwable throwable) {
                String error = pairingError("检查批准失败", throwable);
                pairingActionInProgress = false;
                runOnUiThread(() -> showConnectionView(hostInput, portInput, hermesPortInput, error));
            }
        });
    }

    private JSONObject postPairingJson(String url, JSONObject body) throws Exception {
        HttpURLConnection connection = null;
        try {
            String safeUrl = ReaderApiUrlPolicy.resolve(baseUrl, url);
            connection = (HttpURLConnection) new URL(safeUrl).openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(8000);
            connection.setReadTimeout(15000);
            connection.setRequestMethod("POST");
            connection.setDoOutput(true);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(bytes.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(bytes);
            }
            int code = connection.getResponseCode();
            String responseText = readResponseText(connection, code);
            if (code < 200 || code >= 300) {
                throw new IOException("Mac 返回 HTTP " + code);
            }
            if (responseText.trim().isEmpty()) {
                throw new IOException("Mac 没有返回配对结果");
            }
            return new JSONObject(responseText);
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private void rememberConnectionInputs(
            String normalized,
            String hostInput,
            String portInput,
            String hermesPortInput
    ) {
        baseUrl = normalized;
        prefs.edit()
                .putString(KEY_HOST, hostInput == null ? "" : hostInput.trim())
                .putString(KEY_PORT, normalizePort(portInput))
                .putString(KEY_HERMES_PORT, normalizePortWithDefault(hermesPortInput, DEFAULT_HERMES_PORT))
                .apply();
        rememberRecentHost(normalized);
    }

    private void onVerifiedLanOrigin(String origin) {
        if (origin == null || origin.trim().isEmpty()) {
            return;
        }
        baseUrl = origin.trim();
        rememberRecentHost(baseUrl);
        ClickSyncScheduler.enqueueMetadata(this);
        ClickSyncScheduler.replaceMetadataRepairFromStore(this);
        BookImportScheduler.enqueuePending(this);
        if (new TingleCaptureRepository(this).pendingCount() > 0) {
            TingleUploadScheduler.enqueue(this);
        }
    }

    private String pairingError(String action, Throwable throwable) {
        return action + "：" + userFacingUploadError(throwable) + "。离线书架不受影响。";
    }

    private String formatPairingCode(String code) {
        String cleaned = code == null ? "" : code.replaceAll("\\D", "");
        return cleaned.length() == 6 ? cleaned.substring(0, 3) + " " + cleaned.substring(3) : cleaned;
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
        destroyCurrentWebView();
        root.removeAllViews();

        try {
            webView = new WebView(this);
        } catch (Throwable throwable) {
                showConnectionView(
                        prefs.getString(KEY_HOST, ""),
                        prefs.getString(KEY_PORT, DEFAULT_PORT),
                        prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT),
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
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setAllowFileAccessFromFileURLs(false);
        settings.setAllowUniversalAccessFromFileURLs(false);
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
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (request == null || request.getUrl() == null) {
                    return true;
                }
                boolean hasReliableRedirectSignal = Build.VERSION.SDK_INT >= Build.VERSION_CODES.N;
                boolean userInitiatedHttpGet = hasReliableRedirectSignal
                        && request.hasGesture()
                        && !request.isRedirect()
                        && "GET".equalsIgnoreCase(request.getMethod());
                return handleWebNavigation(
                        request.getUrl().toString(),
                        userInitiatedHttpGet
                );
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                // The legacy callback cannot prove a user gesture. Keep external navigation
                // fail-closed while preserving same-origin Click pages.
                return handleWebNavigation(url, false);
            }

            @Override
            public WebResourceResponse shouldInterceptRequest(
                    WebView view,
                    WebResourceRequest request
            ) {
                if (request == null || request.getUrl() == null) {
                    return blockedWebResponse();
                }
                String target = request.getUrl().toString();
                String scheme = request.getUrl().getScheme();
                boolean http = "http".equalsIgnoreCase(scheme)
                        || "https".equalsIgnoreCase(scheme);
                if ((http && !isTrustedReaderUrl(target))
                        || (request.isForMainFrame() && !isTrustedReaderUrl(target))) {
                    return blockedWebResponse();
                }
                return super.shouldInterceptRequest(view, request);
            }

            @Override
            public void onPageStarted(WebView view, String url, Bitmap favicon) {
                if (!isTrustedReaderUrl(url)) {
                    view.stopLoading();
                    denyPendingWebAudioRequest();
                    showBlockedWebNavigation();
                    return;
                }
                super.onPageStarted(view, url, favicon);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request != null
                        && request.isForMainFrame()
                        && request.getUrl() != null
                        && isTrustedReaderUrl(request.getUrl().toString())) {
                    showConnectionView(
                            prefs.getString(KEY_HOST, ""),
                            prefs.getString(KEY_PORT, DEFAULT_PORT),
                            prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT),
                            "页面加载失败，请检查 Mac 服务和网络。"
                    );
                }
            }
        });
        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                if (!isTrustedAudioCaptureRequest(request)) {
                    denyWebPermissionRequest(request);
                    return;
                }
                if (android.os.Build.VERSION.SDK_INT >= 23
                        && checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    denyPendingWebAudioRequest();
                    pendingAudioPermissionRequest = request;
                    requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1001);
                    return;
                }
                try {
                    request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                } catch (Throwable ignored) {
                    request.deny();
                }
            }

            @Override
            public void onPermissionRequestCanceled(PermissionRequest request) {
                if (pendingAudioPermissionRequest == request) {
                    pendingAudioPermissionRequest = null;
                }
                super.onPermissionRequestCanceled(request);
            }

            @Override
            public boolean onShowFileChooser(
                    WebView view,
                    ValueCallback<Uri[]> filePathCallback,
                    WebChromeClient.FileChooserParams fileChooserParams
            ) {
                if (view == null || !isTrustedReaderUrl(view.getUrl())) {
                    filePathCallback.onReceiveValue(null);
                    return false;
                }
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
        ViewCompat.requestApplyInsets(root);

        loadAuthenticatedWebUrl(url);
    }

    private void destroyCurrentWebView() {
        WebView previous = webView;
        webView = null;
        if (previous == null) {
            return;
        }
        try {
            previous.stopLoading();
        } catch (Throwable ignored) {
            // Continue with bridge and client teardown.
        }
        try {
            previous.removeJavascriptInterface("ClickNativeAudio");
        } catch (Throwable ignored) {
            // The bridge may already have been removed.
        }
        try {
            previous.setWebChromeClient(null);
            previous.setWebViewClient(null);
            previous.removeAllViews();
            previous.destroy();
        } catch (Throwable ignored) {
            // Best-effort native WebView teardown; never retain the detached instance.
        }
    }

    private final class NativeAudioBridge {
        @JavascriptInterface
        public boolean isAvailable() {
            return true;
        }

        @JavascriptInterface
        public boolean usesNativeImeResize() {
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
        public void startHermesVoice() {
            runOnUiThread(() -> {
                nativeAudioMode = "hermes_voice";
                nativeAudioReaderBookId = "";
                requestOrStartNativeAudio();
            });
        }

        @JavascriptInterface
        public void stopRecording() {
            runOnUiThread(() -> stopNativeAudioRecording());
        }
    }

    private void requestOrStartNativeAudio() {
        if (!"recording".equals(nativeAudioMode)
                && (baseUrl == null || baseUrl.trim().isEmpty())) {
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
        if (nativeWavRecorder != null && nativeWavRecorder.isRecording()) {
            notifyNativeAudioStarted();
            return;
        }
        try {
            String captureId;
            File audioFile;
            if ("recording".equals(nativeAudioMode)) {
                TingleCaptureRepository.Capture capture =
                        new TingleCaptureRepository(this).beginCapture(ensureDeviceId());
                captureId = capture.captureId;
                audioFile = capture.audioFile;
            } else {
                captureId = UUID.randomUUID().toString();
                File directory = new File(nativeCaptureRoot(), captureId);
                if (!directory.exists() && !directory.mkdirs()) {
                    throw new IOException("cannot create native recording directory");
                }
                audioFile = new File(directory, "original.wav");
                writeNativeCaptureManifest(
                        audioFile,
                        captureId,
                        "recording",
                        0.0,
                        nativeAudioMode,
                        nativeAudioReaderBookId,
                        0,
                        ""
                );
            }
            nativeAudioFile = audioFile;
            nativeAudioCaptureId = captureId;
            ClickWavRecorder recorder = new ClickWavRecorder(
                    "recording".equals(nativeAudioMode)
                            ? () -> runOnUiThread(this::stopNativeTingleAtSizeLimit)
                            : null
            );
            recorder.start(audioFile);
            nativeWavRecorder = recorder;
            notifyNativeAudioStarted();
        } catch (Throwable throwable) {
            File failedAudio = nativeAudioFile;
            String failedCaptureId = nativeAudioCaptureId;
            String failedMode = nativeAudioMode;
            releaseNativeAudioRecorder();
            if ("recording".equals(failedMode) && !failedCaptureId.isEmpty()) {
                try {
                    new TingleCaptureRepository(this).markRecordingFailure(
                            failedCaptureId,
                            throwable
                    );
                } catch (Throwable ignored) {
                    // The private capture directory remains available for recovery.
                }
            } else if (failedAudio != null && failedAudio.length() <= 44L) {
                deleteNativeCapture(failedAudio);
            }
            notifyNativeAudioError("原生录音启动失败：" + compactError(throwable));
        }
    }

    private void stopNativeAudioRecording() {
        ClickWavRecorder recorder = nativeWavRecorder;
        File audioFile = nativeAudioFile;
        String uploadMode = nativeAudioMode;
        String readerBookId = nativeAudioReaderBookId;
        String captureId = nativeAudioCaptureId;
        nativeWavRecorder = null;
        nativeAudioFile = null;
        nativeAudioCaptureId = "";
        nativeAudioMode = "recording";
        nativeAudioReaderBookId = "";
        if (recorder == null || audioFile == null) {
            notifyNativeAudioModeError(uploadMode, "当前没有正在录音");
            return;
        }
        ClickWavRecorder.Result recorded;
        try {
            recorded = recorder.stopAndFinalize();
        } catch (Throwable recordingError) {
            if ("recording".equals(uploadMode)) {
                try {
                    new TingleCaptureRepository(this).markRecordingFailure(
                            captureId,
                            recordingError
                    );
                } catch (Throwable ignored) {
                    // Keep the private capture directory; never delete a Tingle original.
                }
            } else {
                deleteNativeCapture(audioFile);
            }
            notifyNativeAudioModeError(
                    uploadMode,
                    "没有录到可保存的音频：" + compactError(recordingError)
            );
            return;
        }
        double durationSeconds = recorded.durationSeconds;
        if ("recording".equals(uploadMode)) {
            notifyNativeAudioStopping(uploadMode);
            executor.execute(() -> {
                try {
                    new TingleCaptureRepository(this).markReady(captureId, durationSeconds);
                    TingleUploadScheduler.enqueue(this);
                    runOnUiThread(() -> notifyNativeAudioQueued(captureId));
                } catch (Throwable error) {
                    runOnUiThread(() -> notifyNativeAudioError(
                            "原始音频已保存在手机，但待同步记录写入失败：" + compactError(error)
                    ));
                }
            });
            return;
        }
        try {
            writeNativeCaptureManifest(
                    audioFile,
                    captureId,
                    "pending_upload",
                    durationSeconds,
                    uploadMode,
                    readerBookId,
                    0,
                    ""
            );
        } catch (Throwable manifestError) {
            notifyNativeAudioModeError(uploadMode, "录音已保存在手机，但待同步记录写入失败：" + compactError(manifestError));
            return;
        }
        notifyNativeAudioStopping(uploadMode);
        executor.execute(() -> uploadNativeAudio(audioFile, captureId, durationSeconds, uploadMode, readerBookId));
    }

    private void stopNativeTingleAtSizeLimit() {
        if (nativeWavRecorder == null || !"recording".equals(nativeAudioMode)) {
            return;
        }
        Toast.makeText(
                this,
                "录音已达稳定上限，为保证稳定已保存，请另录一段",
                Toast.LENGTH_LONG
        ).show();
        stopNativeAudioRecording();
    }

    private void releaseNativeAudioRecorder() {
        ClickWavRecorder recorder = nativeWavRecorder;
        nativeWavRecorder = null;
        if (recorder != null) {
            recorder.close();
        }
    }

    private void uploadNativeAudio(File audioFile, String captureId, double durationSeconds, String uploadMode, String readerBookId) {
        if ("recording".equals(uploadMode)) {
            // Tingle originals are owned by TingleCaptureRepository and are never deleted here.
            TingleUploadScheduler.enqueue(this);
            return;
        }
        try {
            byte[] bytes = readAllBytes(audioFile);
            if (bytes.length == 0) {
                throw new IOException("empty native recording");
            }
            String localAudioHash = sha256Hex(bytes);
            String captureDeviceId = readNativeCaptureManifest(audioFile).optString("device_id", "").trim();
            if (captureDeviceId.isEmpty()) {
                captureDeviceId = ensureDeviceId();
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
                uploadPath = "/v1/android/audio-notes/transcribe";
            } else if ("hermes_voice".equals(uploadMode)) {
                payload.put("device_id", captureDeviceId);
                payload.put("tts", true);
                callback = "__clickNativeHermesVoiceDidUpload";
                uploadPath = "/v1/voice/message";
            } else {
                payload.put("source_app", "Click");
                payload.put("source_feature", "Tingle");
                payload.put("device_id", captureDeviceId);
                payload.put("client_capture_id", captureId);
            }

            String safeUploadUrl = ReaderApiUrlPolicy.resolve(baseUrl, uploadPath);
            HttpURLConnection connection = (HttpURLConnection) new URL(safeUploadUrl).openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setConnectTimeout(10000);
            connection.setReadTimeout(180000);
            connection.setRequestMethod("POST");
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            connection.setRequestProperty("X-Click-Device-Id", captureDeviceId);
            String nativeAccessToken = accessToken();
            if (!nativeAccessToken.isEmpty()) {
                connection.setRequestProperty("X-Click-Access-Token", nativeAccessToken);
                connection.setRequestProperty("Authorization", "Bearer " + nativeAccessToken);
            }
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
            JSONObject responseJson = new JSONObject(responseText);
            if (!responseJson.optBoolean("ok", false)) {
                throw new IOException("Mac 未确认保存原始音频");
            }
            if ("recording".equals(uploadMode)) {
                JSONObject serverRecording = responseJson.optJSONObject("recording");
                if (serverRecording == null
                        || serverRecording.optString("recording_id", "").trim().isEmpty()
                        || !localAudioHash.equals(serverRecording.optString("audio_hash", ""))) {
                    throw new IOException("Mac 返回的原始音频校验不一致");
                }
            }
            JSONObject result = new JSONObject();
            result.put("ok", true);
            result.put("response_text", responseText);
            result.put("response_json", responseJson);
            postNativeAudioEvent(callback, result);
            deleteNativeCapture(audioFile);
        } catch (Throwable throwable) {
            recordNativeCaptureFailure(audioFile, captureId, durationSeconds, uploadMode, readerBookId, throwable);
            String preservedMessage = "原始音频已保存在手机，恢复连接后会自动重试：" + userFacingUploadError(throwable);
            if ("reader_note".equals(uploadMode)) {
                notifyNativeReaderAudioError("语音备注上传失败，" + preservedMessage);
            } else if ("hermes_voice".equals(uploadMode)) {
                notifyNativeHermesVoiceError("Hermes 语音发送失败，" + preservedMessage);
            } else {
                notifyNativeAudioError("原生录音上传失败，" + preservedMessage);
            }
        }
    }

    private File nativeCaptureRoot() {
        return new File(getFilesDir(), "voice-inbox-pending");
    }

    private int pendingNativeCaptureCount() {
        int count = new TingleCaptureRepository(this).pendingCount();
        File[] captureDirectories = nativeCaptureRoot().listFiles(File::isDirectory);
        if (captureDirectories == null) {
            return count;
        }
        for (File captureDirectory : captureDirectories) {
            File audioFile = new File(captureDirectory, "original.wav");
            JSONObject manifest = readNativeCaptureManifest(audioFile);
            if (audioFile.isFile()
                    && audioFile.length() > 44L
                    && !"recording".equals(manifest.optString("upload_mode", "recording"))) {
                count += 1;
            }
        }
        return count;
    }

    private File nativeCaptureManifest(File audioFile) {
        return new File(audioFile.getParentFile(), "capture.json");
    }

    private JSONObject readNativeCaptureManifest(File audioFile) {
        File manifestFile = nativeCaptureManifest(audioFile);
        if (!manifestFile.isFile()) {
            return new JSONObject();
        }
        try {
            return new JSONObject(new String(readAllBytes(manifestFile), StandardCharsets.UTF_8));
        } catch (Throwable ignored) {
            return new JSONObject();
        }
    }

    private void writeNativeCaptureManifest(
            File audioFile,
            String captureId,
            String state,
            double durationSeconds,
            String uploadMode,
            String readerBookId,
            int attemptCount,
            String lastError
    ) throws IOException {
        File parent = audioFile.getParentFile();
        if (parent == null || (!parent.exists() && !parent.mkdirs())) {
            throw new IOException("cannot create native capture directory");
        }
        JSONObject prior = readNativeCaptureManifest(audioFile);
        String captureDeviceId = prior.optString("device_id", "").trim();
        if (captureDeviceId.isEmpty()) {
            captureDeviceId = ensureDeviceId();
        }
        JSONObject manifest = new JSONObject();
        try {
            manifest.put("schema", "click.android.voice_capture.v1");
            manifest.put("capture_id", captureId);
            manifest.put("device_id", captureDeviceId);
            manifest.put("state", state);
            manifest.put("audio_path", audioFile.getAbsolutePath());
            manifest.put("duration_seconds", durationSeconds);
            manifest.put("upload_mode", uploadMode == null ? "recording" : uploadMode);
            manifest.put("reader_book_id", readerBookId == null ? "" : readerBookId);
            manifest.put("attempt_count", Math.max(0, attemptCount));
            manifest.put("last_error", lastError == null ? "" : lastError);
            manifest.put("created_at_ms", prior.optLong("created_at_ms", System.currentTimeMillis()));
            manifest.put("updated_at_ms", System.currentTimeMillis());
        } catch (Throwable throwable) {
            throw new IOException("cannot encode native capture manifest", throwable);
        }
        File manifestFile = nativeCaptureManifest(audioFile);
        File temporary = new File(parent, "capture.json.tmp");
        try (FileOutputStream output = new FileOutputStream(temporary, false)) {
            output.write(manifest.toString().getBytes(StandardCharsets.UTF_8));
            output.flush();
            output.getFD().sync();
        }
        if (manifestFile.exists() && !manifestFile.delete()) {
            throw new IOException("cannot replace native capture manifest");
        }
        if (!temporary.renameTo(manifestFile)) {
            throw new IOException("cannot commit native capture manifest");
        }
    }

    private void recordNativeCaptureFailure(
            File audioFile,
            String captureId,
            double durationSeconds,
            String uploadMode,
            String readerBookId,
            Throwable error
    ) {
        JSONObject prior = readNativeCaptureManifest(audioFile);
        try {
            writeNativeCaptureManifest(
                    audioFile,
                    captureId,
                    "upload_failed",
                    durationSeconds,
                    uploadMode,
                    readerBookId,
                    prior.optInt("attempt_count", 0) + 1,
                    compactError(error)
            );
        } catch (Throwable ignored) {
            // The audio remains discoverable by directory scan even if the manifest cannot be updated.
        }
    }

    private void syncPendingNativeAudio() {
        if (baseUrl == null || baseUrl.trim().isEmpty()) {
            return;
        }
        executor.execute(() -> {
            try {
                TingleCaptureRepository repository = new TingleCaptureRepository(this);
                repository.recoverInterruptedCaptures();
                if (repository.pendingCount() > 0) {
                    TingleUploadScheduler.enqueue(this);
                }
            } catch (Throwable ignored) {
                // Tingle originals remain private and will be reconsidered on the next app event.
            }
            File rootDirectory = nativeCaptureRoot();
            File[] captureDirectories = rootDirectory.listFiles(File::isDirectory);
            if (captureDirectories == null) {
                return;
            }
            for (File captureDirectory : captureDirectories) {
                File audioFile = new File(captureDirectory, "original.wav");
                if (!audioFile.isFile()) {
                    continue;
                }
                JSONObject manifest = readNativeCaptureManifest(audioFile);
                String uploadMode = manifest.optString("upload_mode", "recording");
                if ("recording".equals(uploadMode)) {
                    continue;
                }
                String captureId = manifest.optString("capture_id", captureDirectory.getName());
                String state = manifest.optString("state", "recording");
                if ("recording".equals(state)) {
                    try {
                        repairInterruptedNativeWav(audioFile);
                    } catch (Throwable error) {
                        recordNativeCaptureFailure(audioFile, captureId, 0.0, "recording", "", error);
                        continue;
                    }
                }
                if (audioFile.length() <= 44L) {
                    continue;
                }
                double durationSeconds = manifest.optDouble("duration_seconds", 0.0);
                if (durationSeconds <= 0.0) {
                    durationSeconds = Math.max(0.1, (audioFile.length() - 44L) / 32000.0);
                }
                uploadNativeAudio(
                        audioFile,
                        captureId,
                        durationSeconds,
                        uploadMode,
                        manifest.optString("reader_book_id", "")
                );
            }
        });
    }

    private void recoverAndSchedulePendingTingle() {
        executor.execute(() -> {
            try {
                TingleCaptureRepository repository = new TingleCaptureRepository(this);
                repository.recoverInterruptedCaptures();
                if (repository.pendingCount() > 0 && ClickSyncConfig.load(this).isReady()) {
                    TingleUploadScheduler.enqueue(this);
                }
            } catch (Throwable ignored) {
                // No polling: a later app start, connection, or pairing event will retry recovery.
            }
        });
    }

    private void repairInterruptedNativeWav(File audioFile) throws IOException {
        ClickWavRecorder.repairHeader(audioFile);
    }

    private void deleteNativeCapture(File audioFile) {
        File directory = audioFile == null ? null : audioFile.getParentFile();
        if (directory == null || !directory.exists()) {
            return;
        }
        File[] children = directory.listFiles();
        if (children != null) {
            for (File child : children) {
                //noinspection ResultOfMethodCallIgnored
                child.delete();
            }
        }
        //noinspection ResultOfMethodCallIgnored
        directory.delete();
    }

    private String sha256Hex(byte[] bytes) throws IOException {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256").digest(bytes);
            char[] alphabet = "0123456789abcdef".toCharArray();
            char[] output = new char[digest.length * 2];
            for (int index = 0; index < digest.length; index++) {
                int value = digest[index] & 0xff;
                output[index * 2] = alphabet[value >>> 4];
                output[index * 2 + 1] = alphabet[value & 0x0f];
            }
            return new String(output);
        } catch (Throwable throwable) {
            throw new IOException("cannot hash native audio", throwable);
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
        } else if ("hermes_voice".equals(nativeAudioMode)) {
            postNativeAudioEvent("__clickNativeHermesVoiceDidStart", payload);
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
        } else if ("hermes_voice".equals(uploadMode)) {
            postNativeAudioEvent("__clickNativeHermesVoiceDidStop", payload);
        }
    }

    private void notifyNativeAudioQueued(String captureId) {
        JSONObject payload = new JSONObject();
        try {
            payload.put("ok", true);
            payload.put("queued", true);
            payload.put("capture_id", captureId == null ? "" : captureId);
        } catch (Throwable ignored) {
            // Static payload.
        }
        postNativeAudioEvent("__clickNativeAudioDidUpload", payload);
    }

    private void notifyNativeAudioModeError(String uploadMode, String message) {
        if ("reader_note".equals(uploadMode)) {
            notifyNativeReaderAudioError(message);
        } else if ("hermes_voice".equals(uploadMode)) {
            notifyNativeHermesVoiceError(message);
        } else {
            notifyNativeAudioError(message);
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
        if ("reader_note".equals(nativeAudioMode)) {
            postNativeAudioEvent("__clickNativeReaderAudioDidError", payload);
        } else if ("hermes_voice".equals(nativeAudioMode)) {
            postNativeAudioEvent("__clickNativeHermesVoiceDidError", payload);
        }
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

    private void notifyNativeHermesVoiceError(String message) {
        JSONObject payload = new JSONObject();
        try {
            payload.put("ok", false);
            payload.put("error", message == null ? "Hermes 语音失败" : message);
        } catch (Throwable ignored) {
            // Static payload.
        }
        postNativeAudioEvent("__clickNativeHermesVoiceDidError", payload);
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
        menu.addView(menuButton("首页", () -> loadAuthenticatedWebUrl(baseUrl + "/home")));
        menu.addView(menuButton("阅读", this::openNativeReader));
        menu.addView(menuButton("Tingle", this::openLocalTingle));
        menu.addView(menuButton("Hermes", this::openHermesIfAvailable));
        menu.addView(menuButton("刷新", () -> webView.reload()));
        menu.addView(menuButton("连接与同步", () -> showConnectionView(
                preferredHost(),
                prefs.getString(KEY_PORT, DEFAULT_PORT),
                prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT),
                ""
        )));

        PopupWindow popup = new PopupWindow(menu, dp(210), ViewGroup.LayoutParams.WRAP_CONTENT, true);
        popup.setOutsideTouchable(true);
        popup.showAsDropDown(anchor, -dp(140), 0);
    }

    private void showPreflightIdentityView(ClickSyncConfig saved, String message) {
        destroyCurrentWebView();
        root.removeAllViews();

        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setGravity(Gravity.CENTER_HORIZONTAL);
        panel.setPadding(dp(28), dp(28), dp(28), dp(28));
        panel.setBackgroundColor(Color.rgb(7, 8, 6));

        TextView title = text("Click 验收身份", 30, Color.WHITE);
        title.setGravity(Gravity.START);
        title.setTypeface(android.graphics.Typeface.DEFAULT_BOLD);
        panel.addView(title, matchWrap());

        String savedOrigin = saved.baseUrl.isEmpty()
                ? "UNCONFIGURED"
                : saved.baseUrl;
        TextView identity = text(
                "当前生效连接："
                        + ("UNCONFIGURED".equals(savedOrigin) ? "尚未保存" : savedOrigin)
                        + "\nClick 设备 ID："
                        + saved.deviceId,
                14,
                Color.rgb(184, 184, 164)
        );
        identity.setContentDescription(connectionIdentityMarker(savedOrigin, saved.deviceId));
        identity.setClickable(false);
        identity.setLongClickable(false);
        identity.setFocusable(false);
        identity.setTextIsSelectable(false);
        identity.setPadding(0, dp(18), 0, dp(10));
        panel.addView(identity, matchWrap());

        TextView status = text(message, 14, Color.rgb(184, 184, 164));
        status.setClickable(false);
        status.setLongClickable(false);
        status.setFocusable(false);
        status.setTextIsSelectable(false);
        panel.addView(status, matchWrap());

        if (!preflightAuthReceipt.isEmpty()) {
            boolean authorized = preflightAuthReceipt.endsWith("|authorized=true");
            TextView receipt = text(
                    authorized
                            ? "验收认证：已通过一次性只读检查"
                            : "验收认证：未通过",
                    14,
                    authorized
                            ? Color.rgb(137, 205, 147)
                            : Color.rgb(236, 170, 93)
            );
            receipt.setContentDescription(preflightAuthReceipt);
            receipt.setClickable(false);
            receipt.setLongClickable(false);
            receipt.setFocusable(false);
            receipt.setTextIsSelectable(false);
            receipt.setPadding(0, dp(12), 0, 0);
            panel.addView(receipt, matchWrap());
        }

        ScrollView scroll = new ScrollView(this);
        scroll.addView(panel);
        root.addView(scroll, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
        ));
    }

    private boolean dispatchPreflightIdentityProbe(Intent intent) {
        String nonce = preflightNonce(intent);
        if (nonce.isEmpty()) {
            return false;
        }
        preflightIdentityProbeMode = true;
        activePreflightNonce = nonce;
        preflightAuthReceipt = "";
        ClickSyncConfig saved = ClickSyncConfig.loadReadOnly(this);
        if (!saved.baseUrl.isEmpty()) {
            baseUrl = saved.baseUrl;
        }
        showPreflightIdentityView(
                saved,
                "正在执行一次性只读认证；不会显示访问令牌。"
        );
        executor.execute(() -> {
            String receipt = authenticatedPreflightReceipt(nonce);
            runOnUiThread(() -> {
                if (!nonce.equals(activePreflightNonce)
                        || isFinishing()
                        || (Build.VERSION.SDK_INT >= 17 && isDestroyed())) {
                    return;
                }
                preflightAuthReceipt = receipt;
                showPreflightIdentityView(
                        saved,
                        receipt.endsWith("|authorized=true")
                                ? "验收只读认证已通过；未显示或保存访问令牌。"
                                : "验收只读认证未通过；请检查保存的连接、设备授权或网络。"
                );
            });
        });
        return true;
    }

    private static String preflightNonce(Intent intent) {
        if (intent == null) {
            return "";
        }
        String nonce = intent.getStringExtra(EXTRA_PREFLIGHT_NONCE);
        return nonce != null && nonce.matches("[0-9a-f]{32}") ? nonce : "";
    }

    private String authenticatedPreflightReceipt(String nonce) {
        ClickSyncConfig saved = ClickSyncConfig.loadReadOnly(this);
        boolean authorized = false;
        HttpURLConnection connection = null;
        try {
            if (saved.isReady()) {
                String statusUrl = ReaderApiUrlPolicy.resolve(
                        saved.baseUrl,
                        "/v1/mobile/access/status"
                );
                connection = (HttpURLConnection) new URL(statusUrl).openConnection();
                connection.setInstanceFollowRedirects(false);
                connection.setUseCaches(false);
                connection.setConnectTimeout(4000);
                connection.setReadTimeout(6000);
                connection.setRequestMethod("GET");
                connection.setRequestProperty("Accept", "application/json");
                connection.setRequestProperty("X-Click-Device-Id", saved.deviceId);
                connection.setRequestProperty(
                        "X-Click-Access-Token",
                        saved.accessToken
                );
                connection.setRequestProperty(
                        "Authorization",
                        "Bearer " + saved.accessToken
                );
                int code = connection.getResponseCode();
                JSONObject response = new JSONObject(readResponseText(connection, code));
                authorized = isStrictPreflightAuthorization(
                        code,
                        response.optBoolean("ok", false),
                        response.optBoolean("authorized", false),
                        response.optString("status", ""),
                        response.optBoolean("token_required", false),
                        saved.deviceId,
                        response.optString("device_id", "")
                );
            }
        } catch (Throwable ignored) {
            authorized = false;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
        return preflightAuthReceiptMarker(
                nonce,
                saved.baseUrl,
                saved.deviceId,
                authorized
        );
    }

    static boolean isStrictPreflightAuthorization(
            int httpCode,
            boolean ok,
            boolean authorized,
            String status,
            boolean tokenRequired,
            String savedDeviceId,
            String responseDeviceId
    ) {
        return httpCode >= 200
                && httpCode < 300
                && ok
                && authorized
                && "authorized".equals(status)
                && tokenRequired
                && savedDeviceId != null
                && savedDeviceId.equals(responseDeviceId);
    }

    static String preflightAuthReceiptMarker(
            String nonce,
            String savedOrigin,
            String deviceId,
            boolean authorized
    ) {
        return PREFLIGHT_AUTH_RECEIPT_PREFIX
                + "|nonce="
                + (nonce == null ? "" : nonce.trim())
                + "|origin="
                + (savedOrigin == null ? "" : savedOrigin.trim())
                + "|device_id="
                + (deviceId == null ? "" : deviceId.trim())
                + "|authorized="
                + authorized;
    }

    private boolean dispatchRemoteFeature(Intent intent) {
        if (intent == null) {
            return false;
        }
        String feature = intent.getStringExtra(EXTRA_REMOTE_FEATURE);
        intent.removeExtra(EXTRA_REMOTE_FEATURE);
        if (REMOTE_TINGLE_HISTORY.equals(feature)) {
            openTingleHistoryIfAvailable();
            return true;
        }
        if (REMOTE_HERMES.equals(feature)) {
            openHermesIfAvailable();
            return true;
        }
        return false;
    }

    private void openLocalTingle() {
        startActivity(new Intent(this, TingleLocalActivity.class));
    }

    private void openTingleHistoryIfAvailable() {
        openRemoteFeature(
                "/tingle",
                "Mac Tingle 历史",
                false,
                "Mac 当前不可访问。手机里的原始录音仍可在 Tingle 本地页查看、播放和编辑；"
                        + "恢复连接后再打开这里查看已处理灵感。"
        );
    }

    private void openHermesIfAvailable() {
        openRemoteFeature(
                "/hermes",
                "Hermes",
                true,
                "Hermes 需要连接在线的 Mac。Android 不会为此启动本地大模型；"
                        + "离线阅读和手机里的 Tingle 录音不受影响。"
        );
    }

    private void openRemoteFeature(
            String path,
            String title,
            boolean requireHermesRuntime,
            String unavailableMessage
    ) {
        if (baseUrl == null || baseUrl.trim().isEmpty()) {
            showRemoteFeatureUnavailable(title, unavailableMessage, path, requireHermesRuntime);
            return;
        }
        if (accessToken().isEmpty()) {
            showRemoteFeaturePairingRequired(
                    title,
                    unavailableMessage + "\n\n请先在连接设置中完成设备配对。"
            );
            return;
        }
        int probe = ++remoteProbeGeneration;
        android.widget.Toast.makeText(this, "正在检查 " + title + "…", android.widget.Toast.LENGTH_SHORT).show();
        executor.execute(() -> {
            boolean available = requireHermesRuntime
                    ? checkHermesHealth()
                    : checkHealth(baseUrl + "/health");
            runOnUiThread(() -> {
                if (probe != remoteProbeGeneration
                        || isFinishing()
                        || (Build.VERSION.SDK_INT >= 17 && isDestroyed())) {
                    return;
                }
                if (available) {
                    showWebView(baseUrl + path);
                } else {
                    showRemoteFeatureUnavailable(
                            title,
                            unavailableMessage,
                            path,
                            requireHermesRuntime
                    );
                }
            });
        });
    }

    private void showRemoteFeatureUnavailable(
            String title,
            String message,
            String path,
            boolean requireHermesRuntime
    ) {
        if (isFinishing() || (Build.VERSION.SDK_INT >= 17 && isDestroyed())) {
            return;
        }
        new android.app.AlertDialog.Builder(this)
                .setTitle(title + " 暂不可用")
                .setMessage(message)
                .setPositiveButton("重试", (dialog, which) -> openRemoteFeature(
                        path,
                        title,
                        requireHermesRuntime,
                        message
                ))
                .setNeutralButton("连接设置", (dialog, which) -> showConnectionView(
                        preferredHost(),
                        prefs.getString(KEY_PORT, DEFAULT_PORT),
                        prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT),
                        title + " 尚未连接；离线功能不受影响。"
                ))
                .setNegativeButton("关闭", null)
                .show();
    }

    private void showRemoteFeaturePairingRequired(String title, String message) {
        if (isFinishing() || (Build.VERSION.SDK_INT >= 17 && isDestroyed())) {
            return;
        }
        new android.app.AlertDialog.Builder(this)
                .setTitle(title + " 需要配对")
                .setMessage(message)
                .setPositiveButton("连接设置", (dialog, which) -> showConnectionView(
                        preferredHost(),
                        prefs.getString(KEY_PORT, DEFAULT_PORT),
                        prefs.getString(KEY_HERMES_PORT, DEFAULT_HERMES_PORT),
                        "请完成设备配对；离线功能不受影响。"
                ))
                .setNegativeButton("关闭", null)
                .show();
    }

    private boolean checkHermesHealth() {
        HttpURLConnection connection = null;
        try {
            String healthUrl = ReaderApiUrlPolicy.resolve(baseUrl, "/v1/runtime/health");
            connection = (HttpURLConnection) new URL(healthUrl).openConnection();
            connection.setInstanceFollowRedirects(false);
            connection.setUseCaches(false);
            connection.setConnectTimeout(4000);
            connection.setReadTimeout(6000);
            connection.setRequestMethod("GET");
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("X-Click-Device-Id", ensureDeviceId());
            String token = accessToken();
            if (!token.isEmpty()) {
                connection.setRequestProperty("Authorization", "Bearer " + token);
            }
            int code = connection.getResponseCode();
            if (code < 200 || code >= 300) {
                return false;
            }
            JSONObject response = new JSONObject(readResponseText(connection, code));
            return response.optBoolean("ok", false);
        } catch (Throwable ignored) {
            return false;
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private boolean handleWebNavigation(String url, boolean allowExternalHttpGet) {
        if (isTrustedReaderUrl(url)) {
            return maybeOpenNativeFeature(url);
        }
        if (isSafeExternalHttpUrl(url) && allowExternalHttpGet) {
            openExternalHttpUrl(url);
            return true;
        }
        showBlockedWebNavigation();
        return true;
    }

    private boolean maybeOpenNativeFeature(String url) {
        if (maybeOpenNativeReader(url)) {
            return true;
        }
        Uri uri = Uri.parse(url);
        String path = uri.getPath();
        if ("/tingle".equals(path)) {
            openLocalTingle();
            return true;
        }
        if ("/hermes".equals(path)) {
            openHermesIfAvailable();
            return true;
        }
        return false;
    }

    private boolean isTrustedReaderUrl(String url) {
        return baseUrl != null
                && url != null
                && ReaderApiUrlPolicy.sameOrigin(baseUrl, url);
    }

    private boolean isSafeExternalHttpUrl(String url) {
        if (url == null) {
            return false;
        }
        try {
            Uri uri = Uri.parse(url);
            String scheme = uri.getScheme();
            return ("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme))
                    && uri.getHost() != null
                    && !uri.getHost().trim().isEmpty()
                    && uri.getUserInfo() == null;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private void openExternalHttpUrl(String url) {
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
            intent.addCategory(Intent.CATEGORY_BROWSABLE);
            startActivity(intent);
        } catch (Throwable ignored) {
            showBlockedWebNavigation();
        }
    }

    private void showBlockedWebNavigation() {
        if (isFinishing() || (Build.VERSION.SDK_INT >= 17 && isDestroyed())) {
            return;
        }
        android.widget.Toast.makeText(
                this,
                "已阻止非 Click 同源页面在应用内打开",
                android.widget.Toast.LENGTH_SHORT
        ).show();
    }

    private WebResourceResponse blockedWebResponse() {
        return new WebResourceResponse(
                "text/plain",
                StandardCharsets.UTF_8.name(),
                new ByteArrayInputStream(new byte[0])
        );
    }

    private boolean maybeOpenNativeReader(String url) {
        if (!isTrustedReaderUrl(url)) {
            return false;
        }
        Uri uri = Uri.parse(url);
        String path = uri.getPath();
        if (path == null) {
            return false;
        }
        boolean readingRoute = "/library".equals(path) || "/lan/reader".equals(path);
        if (!readingRoute) {
            return false;
        }
        String ui = uri.getQueryParameter("ui");
        if ("web".equalsIgnoreCase(ui) || "modern".equalsIgnoreCase(ui)) {
            return false;
        }
        openNativeReader();
        return true;
    }

    private boolean isTrustedAudioCaptureRequest(PermissionRequest request) {
        if (request == null || request.getOrigin() == null
                || !isTrustedReaderUrl(request.getOrigin().toString())
                || webView == null
                || !isTrustedReaderUrl(webView.getUrl())) {
            return false;
        }
        String[] resources = request.getResources();
        if (resources == null) {
            return false;
        }
        for (String resource : resources) {
            if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) {
                return true;
            }
        }
        return false;
    }

    private void denyPendingWebAudioRequest() {
        PermissionRequest pending = pendingAudioPermissionRequest;
        pendingAudioPermissionRequest = null;
        denyWebPermissionRequest(pending);
    }

    private void denyWebPermissionRequest(PermissionRequest request) {
        if (request == null) {
            return;
        }
        try {
            request.deny();
        } catch (Throwable ignored) {
            // WebView may have already canceled or expired this request.
        }
    }

    private void openNativeReader() {
        openNativeReader("", "");
    }

    private void openNativeReader(String importedBookId, String importedSourceKind) {
        Intent intent = new Intent(this, ClickNativeReaderActivity.class);
        intent.putExtra(ClickNativeReaderActivity.EXTRA_BASE_URL, baseUrl == null ? "" : baseUrl);
        intent.putExtra(ClickNativeReaderActivity.EXTRA_DEVICE_ID, ensureDeviceId());
        intent.putExtra(ClickNativeReaderActivity.EXTRA_ACCESS_TOKEN, accessToken());
        if (importedBookId != null && !importedBookId.trim().isEmpty()) {
            intent.putExtra(ClickNativeReaderActivity.EXTRA_IMPORTED_BOOK_ID, importedBookId.trim());
            intent.putExtra(
                    ClickNativeReaderActivity.EXTRA_IMPORTED_SOURCE_KIND,
                    importedSourceKind == null ? "" : importedSourceKind.trim()
            );
        }
        startActivity(intent);
    }

    private void openBookImport() {
        try {
            startActivityForResult(
                    new Intent(this, BookImportActivity.class),
                    REQUEST_BOOK_IMPORT
            );
        } catch (Throwable error) {
            android.widget.Toast.makeText(
                    this,
                    "系统文件选择器暂时无法打开",
                    android.widget.Toast.LENGTH_SHORT
            ).show();
        }
    }

    private Button menuButton(String label, Runnable action) {
        Button button = new Button(this);
        button.setText(label);
        button.setAllCaps(false);
        button.setGravity(Gravity.START | Gravity.CENTER_VERTICAL);
        ClickUi.styleSecondaryButton(button, interfaceDarkMode);
        button.setPadding(dp(16), 0, dp(16), 0);
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
            loadAuthenticatedWebUrl(baseUrl + "/home");
            return;
        }
        // Keep the app open on the workspace home; Android back/edge gesture should not immediately quit.
    }

    private String normalizeBaseUrl(String hostInput, String portInput) {
        String normalized = ClickSyncConfig.normalizeBaseUrl(hostInput, portInput);
        return normalized.isEmpty() ? null : normalized;
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

    private void initializeCredentialStore() {
        credentialStore = new AndroidCredentialStore(prefs);
        try {
            credentialStore.migrateLegacyAccessToken(LEGACY_KEY_ACCESS_TOKEN);
            credentialStoreMessage = "";
        } catch (Throwable ignored) {
            prefs.edit().remove(LEGACY_KEY_ACCESS_TOKEN).commit();
            credentialStoreMessage = "旧版明文凭据无法迁移，已安全删除；请重新配对。";
        }
    }

    private String ensureDeviceId() {
        return ClickSyncConfig.ensureDeviceId(this);
    }

    private String accessToken() {
        if (credentialStore == null) {
            return "";
        }
        try {
            return credentialStore.accessToken();
        } catch (Throwable ignored) {
            credentialStoreMessage = "Android 系统安全区暂时不可用；离线书架不受影响。";
            return "";
        }
    }

    static String connectionIdentityMarker(String savedOrigin, String deviceId) {
        String origin = savedOrigin == null || savedOrigin.trim().isEmpty()
                ? "UNCONFIGURED"
                : savedOrigin.trim();
        String identity = deviceId == null ? "" : deviceId.trim();
        return PREFLIGHT_CONNECTION_IDENTITY_PREFIX
                + "|origin="
                + origin
                + "|device_id="
                + identity;
    }

    private String pairingSecret() {
        if (credentialStore == null) {
            return "";
        }
        try {
            return credentialStore.pairingSecret();
        } catch (Throwable ignored) {
            credentialStoreMessage = "Android 系统安全区暂时不可用；离线书架不受影响。";
            return "";
        }
    }

    private void loadAuthenticatedWebUrl(String url) {
        if (webView == null || url == null || url.trim().isEmpty()) {
            return;
        }
        String safeUrl;
        try {
            safeUrl = ReaderApiUrlPolicy.resolve(baseUrl, url);
        } catch (RuntimeException error) {
            android.widget.Toast.makeText(
                    this,
                    "已阻止不安全的 Click 页面地址",
                    android.widget.Toast.LENGTH_SHORT
            ).show();
            return;
        }
        Map<String, String> headers = new HashMap<>();
        headers.put("X-Click-Device-Id", ensureDeviceId());
        String token = accessToken();
        if (!token.isEmpty()) {
            headers.put("Authorization", "Bearer " + token);
        }
        webView.loadUrl(safeUrl, headers);
    }

    private String preferredHost() {
        String saved = prefs.getString(KEY_HOST, "");
        if (saved != null && !saved.trim().isEmpty()) {
            return saved;
        }
        return defaultHost();
    }

    private TextView label(String value) {
        TextView view = text(value, 13, ClickUi.secondaryLabel(this, interfaceDarkMode));
        view.setPadding(0, dp(14), 0, dp(6));
        return view;
    }

    private EditText field(String hint) {
        EditText editText = new EditText(this);
        editText.setHint(hint);
        editText.setSingleLine(true);
        editText.setTextColor(ClickUi.primaryLabel(this, interfaceDarkMode));
        editText.setHintTextColor(ClickUi.secondaryLabel(this, interfaceDarkMode));
        editText.setBackground(ClickUi.rounded(
                ClickUi.surface(this, interfaceDarkMode),
                dp(14),
                ClickUi.separator(this, interfaceDarkMode),
                1
        ));
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
