package com.click.shell;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public final class SyncEvidenceActivityTest {
    private static final String NONCE = "0123456789abcdef0123456789abcdef";

    @Test
    public void acceptsOnlyBoundedConsumptionEvidenceRequests() {
        assertTrue(valid("book", "book-one"));
        assertTrue(valid("position", "book-one"));
        assertTrue(valid("annotation", "annotation-one"));

        assertFalse(SyncEvidenceActivity.validRequest("short", 17L, 2L, "book", "book-one"));
        assertFalse(SyncEvidenceActivity.validRequest(NONCE, 0L, 2L, "book", "book-one"));
        assertFalse(SyncEvidenceActivity.validRequest(NONCE, 17L, 0L, "book", "book-one"));
        assertFalse(valid("library_state", "book-one"));
        assertFalse(valid("book_asset", "book-one"));
        assertFalse(valid("audio_note", "audio-one"));
        assertFalse(valid("book", "../unsafe"));
        assertFalse(valid("book", "id|injected=true"));
    }

    @Test
    public void markerContainsOnlyNonceAndReadOnlyDatabaseEvidence() {
        String marker = SyncEvidenceActivity.marker(
                NONCE,
                17L,
                "annotation",
                "annotation-one",
                true,
                2L
        );
        assertEquals(
                "CLICK_SYNC_EVIDENCE"
                        + "|nonce="
                        + NONCE
                        + "|sequence_cursor=17"
                        + "|resource_type=annotation"
                        + "|resource_id=annotation-one"
                        + "|present=true"
                        + "|server_version=2",
                marker
        );
        assertFalse(marker.toLowerCase().contains("token"));
    }

    private static boolean valid(String resourceType, String resourceId) {
        return SyncEvidenceActivity.validRequest(
                NONCE,
                17L,
                2L,
                resourceType,
                resourceId
        );
    }
}
