package com.click.shell;

import android.app.Activity;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.Signature;
import android.os.Bundle;
import android.widget.TextView;

import org.json.JSONObject;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;

/** Debug-only end-to-end probe: signed newer APK manifest -> download -> verifier -> installer. */
public final class AppUpdateProbeActivity extends Activity {
    static final String EXTRA_APK_PATH = "apk_path";
    static final String EXTRA_LAUNCH_INSTALLER = "launch_installer";
    static final String RESULT_FILE = "click-app-update-probe-result.json";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        TextView status = new TextView(this);
        status.setText("Click app update probe is running");
        status.setTextSize(18f);
        status.setPadding(24, 24, 24, 24);
        setContentView(status);
        Thread worker = new Thread(() -> runProbe(status), "click-app-update-probe");
        worker.setDaemon(true);
        worker.start();
    }

    private void runProbe(TextView status) {
        JSONObject result = new JSONObject();
        FakeUpdateServer server = null;
        try {
            result.put("schema", "click.android.app_update.dynamic_probe.v1");
            File apk = new File(getIntent().getStringExtra(EXTRA_APK_PATH));
            if (!apk.isFile() || apk.length() <= 1024) {
                throw new IllegalStateException("newer probe APK is missing");
            }
            PackageInfo candidate = getPackageManager().getPackageArchiveInfo(
                    apk.getAbsolutePath(),
                    PackageManager.GET_SIGNING_CERTIFICATES
            );
            if (candidate == null || candidate.signingInfo == null) {
                throw new IllegalStateException("newer probe APK identity is unreadable");
            }
            Signature[] signatures = candidate.signingInfo.getApkContentsSigners();
            if (signatures == null || signatures.length != 1) {
                throw new IllegalStateException("newer probe APK signer count is invalid");
            }
            String certificate = hex(
                    MessageDigest.getInstance("SHA-256").digest(signatures[0].toByteArray())
            );
            server = new FakeUpdateServer(
                    apk,
                    candidate.getLongVersionCode(),
                    candidate.versionName,
                    VerifiedAssetFile.sha256(apk),
                    certificate
            );
            FakeUpdateServer activeServer = server;
            ClickAppUpdater.checkForTest(
                    this,
                    "http://127.0.0.1:" + server.port,
                    "update-probe-device",
                    "update-probe-token",
                    getIntent().getBooleanExtra(EXTRA_LAUNCH_INSTALLER, false),
                    (state, detail, downloaded) -> {
                        try {
                            boolean ok = "downloaded".equals(state)
                                    && downloaded != null
                                    && downloaded.isFile()
                                    && downloaded.length() == apk.length()
                                    && activeServer.manifestRequests.get() == 1
                                    && activeServer.apkRequests.get() == 1
                                    && activeServer.authenticatedRequests.get() == 2;
                            JSONObject completed = new JSONObject()
                                    .put("schema", "click.android.app_update.dynamic_probe.v1")
                                    .put("ok", ok)
                                    .put("state", state)
                                    .put("detail", detail)
                                    .put("installed_version_code", BuildConfig.VERSION_CODE)
                                    .put("candidate_version_code", candidate.getLongVersionCode())
                                    .put("downloaded_bytes", downloaded == null ? 0 : downloaded.length())
                                    .put("manifest_requests", activeServer.manifestRequests.get())
                                    .put("apk_requests", activeServer.apkRequests.get())
                                    .put("authenticated_requests", activeServer.authenticatedRequests.get())
                                    .put("installer_requested", getIntent().getBooleanExtra(EXTRA_LAUNCH_INSTALLER, false));
                            writeResult(completed);
                            status.setText(ok ? "PASS" : "FAIL\n" + completed);
                        } catch (Throwable error) {
                            status.setText("FAIL\n" + error.getMessage());
                        } finally {
                            activeServer.close();
                        }
                    }
            );
            return;
        } catch (Throwable error) {
            try {
                result.put("ok", false).put(
                        "error",
                        error.getClass().getSimpleName() + ": " + error.getMessage()
                );
            } catch (Throwable ignored) {
                // JSONObject allocation succeeded, so this is only defensive.
            }
        }
        if (server != null) {
            server.close();
        }
        try {
            writeResult(result);
        } catch (Throwable ignored) {
            // The visible status still reports the setup failure.
        }
        JSONObject finalResult = result;
        runOnUiThread(() -> status.setText("FAIL\n" + finalResult));
    }

    private static String hex(byte[] bytes) {
        StringBuilder value = new StringBuilder(bytes.length * 2);
        for (byte item : bytes) {
            value.append(String.format(Locale.ROOT, "%02x", item & 0xff));
        }
        return value.toString();
    }

    private void writeResult(JSONObject value) throws Exception {
        try (FileOutputStream output = new FileOutputStream(new File(getFilesDir(), RESULT_FILE), false)) {
            output.write(value.toString(2).getBytes(StandardCharsets.UTF_8));
            output.getFD().sync();
        }
    }

    private static final class FakeUpdateServer implements AutoCloseable {
        private final File apk;
        private final long versionCode;
        private final String versionName;
        private final String apkSha256;
        private final String certificateSha256;
        private final AtomicBoolean running = new AtomicBoolean(true);
        private final ServerSocket socket;
        private final Thread worker;
        final AtomicInteger manifestRequests = new AtomicInteger();
        final AtomicInteger apkRequests = new AtomicInteger();
        final AtomicInteger authenticatedRequests = new AtomicInteger();
        final int port;

        FakeUpdateServer(
                File apk,
                long versionCode,
                String versionName,
                String apkSha256,
                String certificateSha256
        ) throws Exception {
            this.apk = apk;
            this.versionCode = versionCode;
            this.versionName = versionName;
            this.apkSha256 = apkSha256;
            this.certificateSha256 = certificateSha256;
            socket = new ServerSocket(0, 8, InetAddress.getByName("127.0.0.1"));
            port = socket.getLocalPort();
            worker = new Thread(this::serve, "click-fake-app-update");
            worker.setDaemon(true);
            worker.start();
        }

        private void serve() {
            while (running.get()) {
                try {
                    handle(socket.accept());
                } catch (Throwable error) {
                    if (running.get()) {
                        // The probe timeout will surface a missing response.
                    }
                }
            }
        }

        private void handle(Socket client) throws Exception {
            try (Socket connection = client) {
                BufferedInputStream input = new BufferedInputStream(connection.getInputStream());
                String requestLine = readLine(input);
                boolean device = false;
                boolean token = false;
                while (true) {
                    String line = readLine(input);
                    if (line.isEmpty()) {
                        break;
                    }
                    String lower = line.toLowerCase(Locale.ROOT);
                    device |= lower.equals("x-click-device-id: update-probe-device");
                    token |= lower.equals("authorization: bearer update-probe-token");
                }
                if (device && token) {
                    authenticatedRequests.incrementAndGet();
                }
                BufferedOutputStream output = new BufferedOutputStream(connection.getOutputStream());
                if (!device || !token) {
                    write(output, 401, "application/json", "{\"ok\":false}".getBytes(StandardCharsets.UTF_8));
                } else if (requestLine.startsWith("GET /v1/android/app-update?")) {
                    manifestRequests.incrementAndGet();
                    byte[] body = new JSONObject()
                            .put("ok", true)
                            .put("schema", ClickAppUpdateDescriptor.SCHEMA)
                            .put("available", true)
                            .put("version_code", versionCode)
                            .put("version_name", versionName)
                            .put("apk_url", "/v1/android/app-update/probe/apk")
                            .put("apk_bytes", apk.length())
                            .put("apk_sha256", apkSha256)
                            .put("certificate_sha256", certificateSha256)
                            .put("min_sdk", 28)
                            .toString()
                            .getBytes(StandardCharsets.UTF_8);
                    write(output, 200, "application/json", body);
                } else if (requestLine.startsWith("GET /v1/android/app-update/probe/apk ")) {
                    apkRequests.incrementAndGet();
                    writeHeaders(output, 200, "application/vnd.android.package-archive", apk.length());
                    try (InputStream source = new FileInputStream(apk)) {
                        source.transferTo(output);
                    }
                    output.flush();
                } else {
                    write(output, 404, "text/plain", "not found".getBytes(StandardCharsets.UTF_8));
                }
            }
        }

        private static void write(OutputStream output, int status, String type, byte[] body) throws Exception {
            writeHeaders(output, status, type, body.length);
            output.write(body);
            output.flush();
        }

        private static void writeHeaders(OutputStream output, int status, String type, long length) throws Exception {
            String reason = status == 200 ? "OK" : status == 401 ? "Unauthorized" : "Not Found";
            output.write((
                    "HTTP/1.1 " + status + " " + reason + "\r\n"
                            + "Content-Type: " + type + "\r\n"
                            + "Content-Length: " + length + "\r\n"
                            + "Connection: close\r\n\r\n"
            ).getBytes(StandardCharsets.US_ASCII));
        }

        private static String readLine(BufferedInputStream input) throws Exception {
            java.io.ByteArrayOutputStream bytes = new java.io.ByteArrayOutputStream();
            while (true) {
                int value = input.read();
                if (value < 0 || value == '\n') {
                    break;
                }
                if (value != '\r') {
                    bytes.write(value);
                }
            }
            return bytes.toString(StandardCharsets.US_ASCII);
        }

        @Override
        public void close() {
            running.set(false);
            try {
                socket.close();
            } catch (Throwable ignored) {
            }
            try {
                worker.join(2_000L);
            } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
            }
        }
    }
}
