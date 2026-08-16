package com.click.shell;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.os.Bundle;
import android.view.Gravity;
import android.view.ViewGroup;
import android.widget.LinearLayout;
import android.widget.TextView;

public final class HermesEvidenceActivity extends Activity {
    static final String EXTRA_NONCE = "click_hermes_evidence_nonce";

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        Intent intent = getIntent();
        String nonce = intent == null ? "" : intent.getStringExtra(EXTRA_NONCE);
        nonce = nonce == null ? "" : nonce.trim();
        if (!HermesEvidenceStore.validNonce(nonce)) {
            setContentView(evidenceView(
                    "无效的只读 Hermes 证据请求",
                    "CLICK_HERMES_EVIDENCE_INVALID"
            ));
            return;
        }

        LinearLayout panel = new LinearLayout(this);
        panel.setOrientation(LinearLayout.VERTICAL);
        panel.setGravity(Gravity.CENTER);
        panel.setPadding(48, 48, 48, 48);
        panel.setBackgroundColor(Color.rgb(7, 8, 6));

        HermesEvidenceStore.OnlineReceipt online =
                HermesEvidenceStore.readSuccessfulReceipt(this);
        HermesEvidenceStore.OfflineAttempt offline =
                HermesEvidenceStore.readOfflineAttempt(this);
        if (online != null) {
            panel.addView(
                    evidenceView(
                            "最近一次问当前书成功回执\n"
                                    + online.bookId
                                    + "\n"
                                    + online.receivedAt,
                            HermesEvidenceStore.onlineMarker(nonce, online)
                    ),
                    rowParams()
            );
        }
        if (offline != null) {
            panel.addView(
                    evidenceView(
                            "最近一次问当前书离线阻断\n"
                                    + offline.bookId
                                    + "\n"
                                    + offline.attemptedAt,
                            HermesEvidenceStore.offlineMarker(nonce, offline)
                    ),
                    rowParams()
            );
        }
        if (online == null && offline == null) {
            panel.addView(
                    evidenceView(
                            "尚无问当前书证据",
                            "CLICK_HERMES_EVIDENCE_EMPTY|nonce=" + nonce
                    ),
                    rowParams()
            );
        }
        setContentView(panel);
    }

    private LinearLayout.LayoutParams rowParams() {
        return new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.WRAP_CONTENT
        );
    }

    private TextView evidenceView(String text, String marker) {
        TextView view = new TextView(this);
        view.setText(text);
        view.setContentDescription(marker);
        view.setTextSize(17);
        view.setTextColor(Color.WHITE);
        view.setGravity(Gravity.CENTER);
        view.setPadding(24, 32, 24, 32);
        view.setClickable(false);
        view.setLongClickable(false);
        view.setFocusable(false);
        view.setTextIsSelectable(false);
        return view;
    }
}
