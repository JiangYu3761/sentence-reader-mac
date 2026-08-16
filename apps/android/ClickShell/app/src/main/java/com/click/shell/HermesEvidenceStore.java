package com.click.shell;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONObject;

import java.time.Instant;
import java.util.Locale;
import java.util.UUID;
import java.util.regex.Pattern;

final class HermesEvidenceStore {
    static final String ONLINE_MARKER = "CLICK_HERMES_EVIDENCE";
    static final String OFFLINE_MARKER = "CLICK_HERMES_OFFLINE_EVIDENCE";

    private static final String PREFERENCES = "click_hermes_evidence_v1";
    private static final String RECEIPT_SCHEMA = "click.reader.hermes_request_evidence.v1";
    private static final Pattern NONCE = Pattern.compile("[0-9a-fA-F]{32}");
    private static final Pattern REQUEST_ID = Pattern.compile("hreq_[0-9a-f]{32}");
    private static final Pattern ATTEMPT_ID = Pattern.compile("hattempt_[0-9a-f]{32}");
    private static final Pattern SHA256 = Pattern.compile("[0-9a-f]{64}");
    private static final Pattern SAFE_ID = Pattern.compile("[A-Za-z0-9._:-]{1,200}");
    private static final Pattern SAFE_TIME = Pattern.compile("[A-Za-z0-9._:+-]{1,80}");

    private HermesEvidenceStore() {
    }

    static boolean recordSuccessfulReceipt(Context context, JSONObject rawReceipt) {
        OnlineReceipt receipt = parseSuccessfulReceipt(rawReceipt);
        if (context == null || receipt == null) {
            return false;
        }
        return preferences(context).edit()
                .putString("online_request_id", receipt.requestId)
                .putString("online_received_at", receipt.receivedAt)
                .putString("online_device_id", receipt.deviceId)
                .putString("online_book_id", receipt.bookId)
                .putString("online_context_sha256", receipt.contextSha256)
                .putLong("online_process_id", receipt.processId)
                .putBoolean("online_current_book", true)
                .putBoolean("online_hermes_called", true)
                .putString("online_status", "success")
                .putBoolean("online_response_delivered", true)
                .commit();
    }

    static OfflineAttempt recordPairedOfflineBlocked(Context context, String bookId) {
        OfflineAttempt attempt = offlineAttempt(
                "hattempt_" + UUID.randomUUID().toString().replace("-", "").toLowerCase(Locale.ROOT),
                Instant.now().toString(),
                bookId
        );
        if (context == null || attempt == null) {
            return null;
        }
        boolean stored = preferences(context).edit()
                .putString("offline_attempt_id", attempt.attemptId)
                .putString("offline_attempted_at", attempt.attemptedAt)
                .putString("offline_book_id", attempt.bookId)
                .putBoolean("offline_current_book", true)
                .putBoolean("offline_endpoint_configured", true)
                .putBoolean("offline_credentials_configured", true)
                .putBoolean("offline_network_available", false)
                .putBoolean("offline_request_sent", false)
                .putBoolean("offline_local_fallback", false)
                .commit();
        return stored ? attempt : null;
    }

    static OnlineReceipt readSuccessfulReceipt(Context context) {
        SharedPreferences source = preferences(context);
        OnlineReceipt receipt = new OnlineReceipt(
                source.getString("online_request_id", ""),
                source.getString("online_received_at", ""),
                source.getString("online_device_id", ""),
                source.getString("online_book_id", ""),
                source.getString("online_context_sha256", ""),
                source.getLong("online_process_id", 0L)
        );
        return validOnline(receipt) ? receipt : null;
    }

    static OfflineAttempt readOfflineAttempt(Context context) {
        SharedPreferences source = preferences(context);
        if (!source.getBoolean("offline_endpoint_configured", false)
                || !source.getBoolean("offline_credentials_configured", false)
                || source.getBoolean("offline_network_available", true)
                || source.getBoolean("offline_request_sent", true)
                || source.getBoolean("offline_local_fallback", true)
                || !source.getBoolean("offline_current_book", false)) {
            return null;
        }
        return offlineAttempt(
                source.getString("offline_attempt_id", ""),
                source.getString("offline_attempted_at", ""),
                source.getString("offline_book_id", "")
        );
    }

    static OnlineReceipt parseSuccessfulReceipt(JSONObject rawReceipt) {
        if (rawReceipt == null
                || !RECEIPT_SCHEMA.equals(rawReceipt.optString("schema"))
                || !rawReceipt.optBoolean("current_book", false)
                || !rawReceipt.optBoolean("hermes_called", false)
                || !"success".equals(rawReceipt.optString("status"))
                || !rawReceipt.optBoolean("response_delivered", false)) {
            return null;
        }
        OnlineReceipt receipt = new OnlineReceipt(
                rawReceipt.optString("request_id").trim(),
                rawReceipt.optString("received_at").trim(),
                rawReceipt.optString("device_id").trim(),
                rawReceipt.optString("book_id").trim(),
                rawReceipt.optString("context_sha256").trim(),
                rawReceipt.optLong("process_id", 0L)
        );
        return validOnline(receipt) ? receipt : null;
    }

    static OfflineAttempt offlineAttempt(String attemptId, String attemptedAt, String bookId) {
        OfflineAttempt attempt = new OfflineAttempt(
                String.valueOf(attemptId == null ? "" : attemptId).trim(),
                String.valueOf(attemptedAt == null ? "" : attemptedAt).trim(),
                String.valueOf(bookId == null ? "" : bookId).trim()
        );
        return ATTEMPT_ID.matcher(attempt.attemptId).matches()
                && SAFE_TIME.matcher(attempt.attemptedAt).matches()
                && SAFE_ID.matcher(attempt.bookId).matches()
                ? attempt
                : null;
    }

    static boolean validNonce(String nonce) {
        return nonce != null && NONCE.matcher(nonce).matches();
    }

    static boolean isPairedOffline(
            boolean endpointConfigured,
            boolean credentialsConfigured,
            boolean networkAvailable
    ) {
        return endpointConfigured && credentialsConfigured && !networkAvailable;
    }

    static String onlineMarker(String nonce, OnlineReceipt receipt) {
        if (!validNonce(nonce) || !validOnline(receipt)) {
            throw new IllegalArgumentException("invalid Hermes evidence marker");
        }
        return ONLINE_MARKER
                + "|nonce=" + nonce
                + "|request_id=" + receipt.requestId
                + "|received_at=" + receipt.receivedAt
                + "|device_id=" + receipt.deviceId
                + "|current_book=true"
                + "|book_id=" + receipt.bookId
                + "|context_sha256=" + receipt.contextSha256
                + "|process_id=" + receipt.processId
                + "|hermes_called=true"
                + "|status=success"
                + "|response_delivered=true";
    }

    static String offlineMarker(String nonce, OfflineAttempt attempt) {
        if (!validNonce(nonce) || attempt == null) {
            throw new IllegalArgumentException("invalid Hermes offline marker");
        }
        return OFFLINE_MARKER
                + "|nonce=" + nonce
                + "|attempt_id=" + attempt.attemptId
                + "|attempted_at=" + attempt.attemptedAt
                + "|current_book=true"
                + "|book_id=" + attempt.bookId
                + "|endpoint_configured=true"
                + "|credentials_configured=true"
                + "|network_available=false"
                + "|request_sent=false"
                + "|local_fallback=false";
    }

    private static boolean validOnline(OnlineReceipt receipt) {
        return receipt != null
                && REQUEST_ID.matcher(receipt.requestId).matches()
                && SAFE_TIME.matcher(receipt.receivedAt).matches()
                && SAFE_ID.matcher(receipt.deviceId).matches()
                && SAFE_ID.matcher(receipt.bookId).matches()
                && SHA256.matcher(receipt.contextSha256).matches()
                && receipt.processId > 0L;
    }

    private static SharedPreferences preferences(Context context) {
        return context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE);
    }

    static final class OnlineReceipt {
        final String requestId;
        final String receivedAt;
        final String deviceId;
        final String bookId;
        final String contextSha256;
        final long processId;

        OnlineReceipt(
                String requestId,
                String receivedAt,
                String deviceId,
                String bookId,
                String contextSha256,
                long processId
        ) {
            this.requestId = String.valueOf(requestId == null ? "" : requestId);
            this.receivedAt = String.valueOf(receivedAt == null ? "" : receivedAt);
            this.deviceId = String.valueOf(deviceId == null ? "" : deviceId);
            this.bookId = String.valueOf(bookId == null ? "" : bookId);
            this.contextSha256 = String.valueOf(contextSha256 == null ? "" : contextSha256);
            this.processId = processId;
        }
    }

    static final class OfflineAttempt {
        final String attemptId;
        final String attemptedAt;
        final String bookId;

        OfflineAttempt(String attemptId, String attemptedAt, String bookId) {
            this.attemptId = attemptId;
            this.attemptedAt = attemptedAt;
            this.bookId = bookId;
        }
    }
}
