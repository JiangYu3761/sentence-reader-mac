package com.click.shell;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.json.JSONObject;
import org.junit.Test;

public final class HermesEvidenceStoreTest {
    private static final String NONCE = "0123456789abcdef0123456789abcdef";

    @Test
    public void successfulReceiptProducesPrivacySafeNonceBoundMarker() throws Exception {
        JSONObject raw = new JSONObject()
                .put("schema", "click.reader.hermes_request_evidence.v1")
                .put("request_id", "hreq_" + "a".repeat(32))
                .put("received_at", "2026-07-29T00:01:02.123456+00:00")
                .put("device_id", "android-test")
                .put("current_book", true)
                .put("book_id", "book-42")
                .put("context_sha256", "b".repeat(64))
                .put("process_id", 76380)
                .put("hermes_called", true)
                .put("status", "success")
                .put("response_delivered", true)
                .put("question", "must not survive parsing")
                .put("access_token", "must-not-survive");

        HermesEvidenceStore.OnlineReceipt receipt =
                HermesEvidenceStore.parseSuccessfulReceipt(raw);
        assertNotNull(receipt);
        String marker = HermesEvidenceStore.onlineMarker(NONCE, receipt);

        assertTrue(marker.startsWith("CLICK_HERMES_EVIDENCE|nonce=" + NONCE));
        assertTrue(marker.contains("|request_id=hreq_" + "a".repeat(32)));
        assertTrue(marker.contains("|device_id=android-test"));
        assertTrue(marker.contains("|book_id=book-42"));
        assertTrue(marker.contains("|context_sha256=" + "b".repeat(64)));
        assertTrue(marker.contains("|process_id=76380"));
        assertTrue(marker.endsWith(
                "|hermes_called=true|status=success|response_delivered=true"
        ));
        assertFalse(marker.contains("must not survive"));
        assertFalse(marker.toLowerCase().contains("token"));
        assertFalse(marker.toLowerCase().contains("question"));
    }

    @Test
    public void onlyActualSuccessfulCurrentBookReceiptIsAccepted() throws Exception {
        JSONObject failed = new JSONObject()
                .put("schema", "click.reader.hermes_request_evidence.v1")
                .put("request_id", "hreq_" + "a".repeat(32))
                .put("received_at", "2026-07-29T00:01:02Z")
                .put("device_id", "android-test")
                .put("current_book", true)
                .put("book_id", "book-42")
                .put("context_sha256", "b".repeat(64))
                .put("process_id", 76380)
                .put("hermes_called", true)
                .put("status", "failed")
                .put("response_delivered", true);
        assertNull(HermesEvidenceStore.parseSuccessfulReceipt(failed));

        failed.put("status", "success").put("hermes_called", false);
        assertNull(HermesEvidenceStore.parseSuccessfulReceipt(failed));
    }

    @Test
    public void offlineAttemptProvesNoRequestAndNoLocalFallback() {
        HermesEvidenceStore.OfflineAttempt attempt = HermesEvidenceStore.offlineAttempt(
                "hattempt_" + "c".repeat(32),
                "2026-07-29T00:02:03Z",
                "book-42"
        );
        assertNotNull(attempt);
        String marker = HermesEvidenceStore.offlineMarker(NONCE, attempt);

        assertTrue(marker.startsWith("CLICK_HERMES_OFFLINE_EVIDENCE|nonce=" + NONCE));
        assertTrue(marker.contains("|attempt_id=hattempt_" + "c".repeat(32)));
        assertTrue(marker.contains("|current_book=true|book_id=book-42"));
        assertTrue(marker.contains(
                "|endpoint_configured=true"
                        + "|credentials_configured=true"
                        + "|network_available=false"
        ));
        assertTrue(marker.endsWith("|request_sent=false|local_fallback=false"));
        assertFalse(marker.toLowerCase().contains("token"));
        assertFalse(marker.toLowerCase().contains("question"));
    }

    @Test
    public void offlineEvidenceRequiresPairedConfigurationAndNoNetwork() {
        assertTrue(HermesEvidenceStore.isPairedOffline(true, true, false));
        assertFalse(HermesEvidenceStore.isPairedOffline(false, true, false));
        assertFalse(HermesEvidenceStore.isPairedOffline(true, false, false));
        assertFalse(HermesEvidenceStore.isPairedOffline(true, true, true));
    }

    @Test
    public void nonceAndMarkerValuesRejectInjection() {
        assertTrue(HermesEvidenceStore.validNonce(NONCE));
        assertFalse(HermesEvidenceStore.validNonce("short"));
        assertFalse(HermesEvidenceStore.validNonce(NONCE + "|request_sent=true"));
        assertNull(HermesEvidenceStore.offlineAttempt(
                "hattempt_" + "c".repeat(32),
                "2026-07-29T00:02:03Z",
                "book-42|request_sent=true"
        ));
    }
}
