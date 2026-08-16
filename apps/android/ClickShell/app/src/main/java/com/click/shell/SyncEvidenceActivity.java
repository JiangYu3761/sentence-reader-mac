package com.click.shell;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.view.Gravity;
import android.widget.TextView;

import java.util.regex.Pattern;

public final class SyncEvidenceActivity extends Activity {
    static final String MARKER = "CLICK_SYNC_EVIDENCE";
    static final String EXTRA_NONCE = "click_sync_evidence_nonce";
    static final String EXTRA_EXPECTED_SEQUENCE = "click_sync_expected_sequence";
    static final String EXTRA_EXPECTED_VERSION = "click_sync_expected_version";
    static final String EXTRA_RESOURCE_TYPE = "click_sync_resource_type";
    static final String EXTRA_RESOURCE_ID = "click_sync_resource_id";

    private static final Pattern NONCE = Pattern.compile("[0-9a-fA-F]{32}");
    private static final Pattern RESOURCE_ID = Pattern.compile("[A-Za-z0-9._:-]{3,200}");

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Intent intent = getIntent();
        String nonce = extra(intent, EXTRA_NONCE);
        long expectedSequence = intent == null
                ? 0L
                : intent.getLongExtra(EXTRA_EXPECTED_SEQUENCE, 0L);
        long expectedVersion = intent == null
                ? 0L
                : intent.getLongExtra(EXTRA_EXPECTED_VERSION, 0L);
        String resourceType = extra(intent, EXTRA_RESOURCE_TYPE);
        String resourceId = extra(intent, EXTRA_RESOURCE_ID);
        if (!validRequest(
                nonce,
                expectedSequence,
                expectedVersion,
                resourceType,
                resourceId
        )) {
            showInvalidRequest();
            return;
        }

        ClickNativeStore.SyncEvidence evidence = ClickNativeStore.readSyncEvidence(
                this,
                resourceType,
                resourceId
        );
        TextView result = evidenceTextView();
        result.setText(
                "Click 同步消费证据\n"
                        + resourceType
                        + " / "
                        + resourceId
                        + "\n游标 "
                        + evidence.sequenceCursor
                        + " · 本地版本 "
                        + evidence.serverVersion
        );
        result.setContentDescription(marker(
                nonce,
                evidence.sequenceCursor,
                resourceType,
                resourceId,
                evidence.present,
                evidence.serverVersion
        ));
        setContentView(result);
    }

    static boolean validRequest(
            String nonce,
            long expectedSequence,
            long expectedVersion,
            String resourceType,
            String resourceId
    ) {
        return nonce != null
                && NONCE.matcher(nonce).matches()
                && expectedSequence > 0L
                && expectedVersion > 0L
                && validResourceType(resourceType)
                && resourceId != null
                && RESOURCE_ID.matcher(resourceId).matches();
    }

    static String marker(
            String nonce,
            long sequenceCursor,
            String resourceType,
            String resourceId,
            boolean present,
            long serverVersion
    ) {
        return MARKER
                + "|nonce="
                + nonce
                + "|sequence_cursor="
                + Math.max(0L, sequenceCursor)
                + "|resource_type="
                + resourceType
                + "|resource_id="
                + resourceId
                + "|present="
                + present
                + "|server_version="
                + Math.max(0L, serverVersion);
    }

    private static boolean validResourceType(String resourceType) {
        return "book".equals(resourceType)
                || "position".equals(resourceType)
                || "annotation".equals(resourceType);
    }

    private static String extra(Intent intent, String key) {
        String value = intent == null ? "" : intent.getStringExtra(key);
        return value == null ? "" : value.trim();
    }

    private TextView evidenceTextView() {
        TextView view = new TextView(this);
        view.setTextSize(18);
        view.setTextColor(Color.WHITE);
        view.setBackgroundColor(Color.rgb(7, 8, 6));
        view.setGravity(Gravity.CENTER);
        view.setPadding(48, 48, 48, 48);
        view.setClickable(false);
        view.setLongClickable(false);
        view.setFocusable(false);
        view.setTextIsSelectable(false);
        return view;
    }

    private void showInvalidRequest() {
        TextView invalid = evidenceTextView();
        invalid.setText("无效的只读同步证据请求");
        invalid.setContentDescription("CLICK_SYNC_EVIDENCE_INVALID");
        setContentView(invalid);
    }
}
