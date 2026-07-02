# Sentence Reader Product Acceptance

Updated: 2026-06-29

## Daily-use Product Boundary

Sentence Reader is accepted as a daily-use local reading product when these workflows work without manual database work:

- Open the packaged Mac app from the Dock or build folder.
- Open the app directly into the `书库` main interface, which loads `http://127.0.0.1:18180/library?surface=mac-app` in the main Mac window when Reader API is available.
- Drag and move the Mac window from the red/yellow/green titlebar row; the embedded Library/Vocabulary web surface must not cover that macOS window-control area.
- See previously imported owned EPUB copies after restart; the Library API must rescan `~/Library/Application Support/SentenceReader/Books/*/book.epub` and re-register missing internal books without requiring the original source file.
- See a Library V2 reading-first home page: `继续阅读` is the first task, recent books appear below it, and recent notes/red highlights are visible as knowledge assets.
- Return from正文 to `书库` in the same main window; the app must not open a second library window for normal use.
- See all known books as a cover wall with progress, reading state, note count, and red-highlight count.
- Click a book cover/card to continue reading directly; hover must not promote 收藏/单词/详情/delete into a floating action layer. Secondary actions live below the card, and remove/hide lives in explicit management mode or batch controls.
- Import EPUBs, reveal the internal EPUB copy, export, and remove books from the library list without deleting notes or data.
- Click `导入 EPUB` in the Mac Library WebView and get the native macOS file picker.
- Use dedicated `笔记` and `红标` centers instead of treating them only as book filters.
- Search across book title, author, notes, and red-highlight text.
- Keep the stable library data contract available through `/api/library/dashboard`.
- Continue reading from the Mac App `书库` into the original native sentence reader through `sentence-reader://open-native?book_id=...`, not the iPad/LAN web reader, so direct App opening and library opening render the same book through the same Mac reading surface.
- Keep正文 controls readable and grouped: visible reader chrome is limited to `书库`, `目录`, `笔记`, `设置`, and `更多`; secondary actions belong under `更多`.
- Import EPUB files into Sentence Reader's owned app-support library so the original source file can be deleted or moved after import.
- Read EPUB in a black, Microsoft YaHei reading surface with compact hidden chrome.
- Turn pages horizontally without cropped bottom text or trailing blank pages.
- Move across chapter edges without losing reading position.
- Mark whole sentences red, including multi-line selections.
- Add text notes and voice notes, then see the note again by clicking the sentence.
- Keep the interaction-router contract stable: sentence-level gestures win on sentence text; editing fields and controls still keep system behavior.
- Use single-click/single-tap on an English word for lookup, double-click/double-tap for sentence notes, Mac two-finger tap for whole-sentence red highlight, the iPad bottom action bar for red highlight, `Command+C` for copying selected text, and `Option` + double-click as a backup word-lookup path on pointer devices.
- English lookup must fall back to the general dictionary even when the current book has not generated a book-local vocabulary list.
- If book/domain/local dictionary lookup misses, English lookup may use the Mac-side Hermes/Qwen online fallback, save an `online_lookup` vocab item to the current book, and let the user correct the meaning inline without leaving the lookup card.
- User-corrected lookup meanings must be saved into the current book glossary with `source='user'` and must take priority over dictionary and online lookup results.
- English phrase lookup must preserve selected phrases such as `tree of life` instead of collapsing them into a single unusable token.
- Life-study context meanings must stay book/domain-scoped and must not be imported into the general dictionary.
- Common reading/business words such as `strategy`, `strategies`, `market`, `business`, and `the` must return a dictionary-backed result through `/books/{book_id}/lookup`.
- The local dictionary should include the imported ECDICT-compatible source, not only the compact seed list.
- Keep normal Mac app quitting behavior: `Command+Q` exits Sentence Reader.
- Keep `docs/interaction_contract.md` and `scripts/sentence_reader_interaction_contract_smoke.py` passing before changing any sentence/system gesture boundary.
- Persist books, reading position, highlights, notes, audio-note state, exports, and sync events through Reader API and PostgreSQL.
- Let the user choose Mac voice transcription provider in the UI; Click's software-layer local recognition path (`FunASR`) is the default, and Apple Speech is the backup/system provider.
- Open the iPad LAN reader at `http://<mac-lan-ip>:18180/lan/reader` on the same Wi-Fi.
- Open the iPad LAN library at `http://<mac-lan-ip>:18180/library` on the same Wi-Fi.
- Open the Mobile Workspace home at `http://<mac-lan-ip>:18180/home` on the same Wi-Fi and see only the three primary entries `阅读`, `录音`, and `Hermes`.
- Use the mobile `阅读` entry to keep the existing `/library` and `/lan/reader` reading path.
- Use the mobile `录音` entry to save new recordings into the independent `~/Documents/Recordings` total recording store with schema `local.recordings.audio_asset.v1`; the old Click Knowledge Inbox recording path is legacy read-only compatibility, and recording assets must not be stored in Hermes internal state or Marketplace OS.
- Manage mobile recordings through list/detail/audio, manual title/category/tag editing, non-destructive hide, and reprocess dry-run; manual corrections must not be overwritten by processing unless explicitly allowed.
- Use local mobile access control for phone clients: unapproved devices can request/poll access and view basic connection state, but durable recording upload and Hermes routes require local approval/token when a device ID is present. Mac localhost browser debugging remains available.
- Use the mobile `Hermes` entry for text chat and temporary voice-message upload through the Mac-side Hermes runtime boundary; Hermes voice messages go to VoiceInbox and are separate from durable recording assets.
- Use edge-tts voice reply only when the local edge-tts command is available, with `zh-CN-YunjianNeural` as the default voice; lack of TTS must still return text.
- Example LAN URL format: `http://<mac-lan-ip>:18180/lan/reader`.
- Use the iPad LAN reader with hidden目录 drawer, full-screen正文, swipe/page buttons, persisted highlights/notes/position, bottom sentence action bar, font-size settings, return-to-library control, and clear voice fallback.
- Keep iPad reading chrome compact: top controls must not look like a management toolbar, sentence actions should float only after selection, and hidden controls must not leave a large unused bottom area under the last line.
- Treat Windows as a planned platform route, not part of the current accepted product. The shared Web reader keyboard contract for the Windows route is implemented (`N` note, `R` red highlight, `V` voice note, `Esc`, arrows/PageUp/PageDown), but Windows P1/P2/P3 are documented in `docs/windows_client_plan.md` and must not be described as completed before their own implementation and verification pass.
- Treat Android and iPad shells as local P1 clients, not public signed releases. Android debug APK builds are phone-test artifacts; iPad installation still requires Apple signing and a real-device install path.

## Hard Checks

The product-grade check is not a single manual glance. It requires:

- `./scripts/v1_acceptance.sh`
- `./scripts/v21_ipad_lan_acceptance.sh`
- `.venv-reader-api/bin/python -m pytest tests/test_reader_api_mock.py`
- `.venv-reader-api/bin/python -m compileall reader_api scripts tests`
- `swiftc Probe/NativeSentenceReader/SentenceReaderNative.swift -o /tmp/SentenceReaderNativeProductCheck -framework Cocoa -framework WebKit -framework AVFoundation -framework Speech`
- `python3 scripts/package_sentence_reader_app.py`
- `python3 scripts/public_repo_privacy_smoke.py`
- `python3 scripts/public_readme_platform_smoke.py`
- `python3 scripts/android_shell_static_smoke.py`
- `python3 scripts/mobile_shell_static_smoke.py`
- `python3 scripts/click_mobile_workspace_smoke.py`
- `scripts/build_android_click_shell.sh`
- `python3 scripts/sentence_reader_import_ownership_static_smoke.py`
- `python3 scripts/sentence_reader_interaction_contract_smoke.py`
- `python3 scripts/sentence_reader_vocab_lookup_static_smoke.py`
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_pipeline_smoke.py`
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_import_smoke.py`
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_domain_lookup_smoke.py`
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_book_lookup_smoke.py` after Genesis Life-study is imported
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_review_pack_smoke.py` after Genesis Life-study is imported
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_apply_review_smoke.py` after Genesis review pack exists
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_review_ui_smoke.py` after Genesis review pack exists
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_review_suggestions_smoke.py` after Genesis review pack exists
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_word_review_pack_smoke.py` after Genesis full run exists
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_word_frequency_smoke.py` after Genesis full run exists
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_phrase_uncommon_pack_smoke.py` after Genesis full run exists
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_stage_gate_smoke.py` after Genesis review pack exists
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_frontend_smoke.py`
- `.venv-reader-api/bin/python scripts/lifestudy_corpus_inventory_smoke.py` after next-volume probes start
- `.venv-reader-api/bin/python scripts/lifestudy_master_vocab_aggregate_smoke.py` after at least Genesis/Exodus/Leviticus full no-write outputs exist
- `.venv-reader-api/bin/python scripts/lifestudy_all_words_master_smoke.py`
- `.venv-reader-api/bin/python scripts/lifestudy_all_words_master_full_smoke.py` after the 51-volume all-word table exists
- `.venv-reader-api/bin/python scripts/lifestudy_all_words_chinese_context_candidates_smoke.py` after the 51-volume clean all-word table exists
- `.venv-reader-api/bin/python scripts/lifestudy_dictionary_guided_review_v2_smoke.py` after the dictionary-guided learning review exists
- `.venv-reader-api/bin/python scripts/lifestudy_needs_review_adjudication_v1_smoke.py` after the 2,205-row needs-review adjudication exists
  - Expected current result: 2,205 input rows, 2,205 output rows, 2,205 adjudicated, 0 still needing manual review, 0 database writes, 0 front-end import-ready rows.
- `.venv-reader-api/bin/python scripts/lifestudy_needs_review_frontend_smoke.py` after the 2,205-row needs-review front-end candidate queue exists
- `.venv-reader-api/bin/python scripts/lifestudy_needs_review_frontend_applied_smoke.py` after the first 15 needs-review-derived front-end terms have been explicitly applied
- `.venv-reader-api/bin/python scripts/lifestudy_needs_review_frontend_live_lookup_smoke.py` after Reader API is running and the 15 needs-review-derived terms have been applied
- `.venv-reader-api/bin/python scripts/lifestudy_needs_review_frontend_ui_static_smoke.py` after the Mac/iPad lookup card metadata display is updated
- `scripts/v1_acceptance.sh` and `scripts/v21_ipad_lan_acceptance.sh` must still pass after the Life-study front-end glossary batch is applied; this guards against lookup work breaking the main reader, iPad LAN reader, PostgreSQL, packaging, and product identity surfaces.
- `.venv-reader-api/bin/python scripts/lifestudy_frontend_candidate_review_v2_smoke.py` after the front-end human-review candidate pack exists
- `.venv-reader-api/bin/python scripts/lifestudy_frontend_candidate_adjudication_v2_smoke.py` after the operator-delegated Codex adjudication pack exists
- `.venv-reader-api/bin/python scripts/lifestudy_frontend_candidate_adjudication_apply_smoke.py` after the 26-row adjudicated dry-run boundary exists
- `.venv-reader-api/bin/python scripts/lifestudy_frontend_candidate_adjudication_applied_smoke.py` after the 26-row adjudicated batch has been explicitly applied
- `.venv-reader-api/bin/python scripts/lifestudy_frontend_candidate_adjudication_live_lookup_smoke.py` after Reader API is running and the 26-row adjudicated batch has been applied
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_v1_build_smoke.py`
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_v1_apply_smoke.py`
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_v1_frontend_smoke.py`
- `.venv-reader-api/bin/python scripts/lifestudy_context_vocab_v1_live_lookup_smoke.py` after Reader API is running and V1 apply has completed
- `.venv-reader-api/bin/python scripts/sentence_reader_library_ui_static_smoke.py`
- `.venv-reader-api/bin/python scripts/sentence_reader_library_v2_smoke.py`
- `python3 scripts/probe_readium_publication_open.py --timeout 300` must use the project fixture when the original Desktop EPUB is absent.
- `python3 scripts/check_native_reader.py` must include the `书库` window and non-destructive library-remove markers.
- `python3 scripts/sentence_reader_product_readiness_smoke.py` after the live LAN service is running

## Current Product Decision

The current version is product-grade for local EPUB reading on this Mac and trusted same-LAN iPad browser reading, with a functional native book library manager.

It is not yet a public-network, multi-device account, signed installer, polished visual bookshelf, or Windows client. The iPad native shell P1 exists as a local SwiftUI + WKWebView scaffold, but it is not accepted as a signed/installable iPad product until the iOS destination build, Apple signing, and real-device install checks pass. The Android shell P1 exists as a local Activity + WebView scaffold and can produce a local debug APK, but it is not accepted as a finished Android app until real-device install and same-LAN reading checks pass. Windows is currently a planned route: P1 browser version, P2 WebView2/Tauri `Click.exe`, and P3 installer with shortcut and diagnostics behavior.

Latest verification on 2026-06-25 repeated the hard checks and live readiness smoke without finding a new code-level blocker. This means the right stop condition is to keep the current product boundary stable instead of adding new feature scope.

Repeat verification on 2026-06-25 13:27 CST passed `v1_acceptance.sh`, `v21_ipad_lan_acceptance.sh`, Reader API pytest, Python compileall, Swift compile, packaged app build, and live LAN/FunASR readiness checks. No functional source or PostgreSQL schema change was required in this pass.

Repeat verification on 2026-06-26 added the native `书库` management interface and passed `v1_acceptance.sh`, `v21_ipad_lan_acceptance.sh`, Swift compile, package build, product static smoke, library live smoke, and live product readiness smoke. Current iPad URLs during verification: library `http://<mac-lan-ip>:18180/library`, direct reader `http://<mac-lan-ip>:18180/lan/reader`.

The Library V2 UI adds a reading-first home page, cover endpoint, direct book-card opening, details drawer, note center, red-highlight center, batch actions, and iPad `/library` access. It remains one Sentence Reader system and does not introduce a second reader.

2026-06-26 iPad touch refinement added bottom sentence actions, `Aa` reading settings, red highlight through the sentence action bar, and `书库` return from `/lan/reader` to `/library`. This fixes the product problem where sentence-level operations were too far away in the top toolbar and the reader had no obvious route back to the main interface.

Repeat verification after the iPad touch refinement passed `v1_acceptance.sh`, `v21_ipad_lan_acceptance.sh`, Reader API pytest, Python compileall, Swift compile, package build, iPad static/API smoke, Library V2 smoke, live product readiness smoke, and live `/library` + `/lan/reader` marker checks. Current verified iPad URLs: library `http://<mac-lan-ip>:18180/library`, direct reader `http://<mac-lan-ip>:18180/lan/reader`.

2026-06-26 compact-chrome refinement shortened the iPad toolbar, changed previous/next to arrow controls, compacted the bottom sentence action bar, and removed the permanent large bottom padding from正文. `v21_ipad_lan_acceptance.sh`, static/API smoke, live product readiness smoke, and live compact chrome marker checks passed after the change.

## Stop Condition

Stop this round when:

1. The code changes are limited to stability, persistence, iPad LAN, voice fallback, tests, and docs.
2. The hard checks pass.
3. The live Reader API has exactly one listener on port `18180`, bound to all interfaces.
4. Voice transcription provider selection is available; FunASR is the default software-layer local path, Apple Speech is the backup, and FunASR health is optional unless the selected/default local path is being verified.
5. The native app opens directly to the `书库` main interface in the main window.
6. The native app exposes a real Library V2 interface through `/library`, not only a quick switch menu or fallback table.
7. The first screen has `继续阅读`, recent reading, and recent notes/red highlights; it must not present engineering architecture copy as user-facing content.
8. The native app uses `surface=mac-app` and intercepts `sentence-reader://open-native?book_id=...` clicks from its embedded `/library` surface, then opens the selected book in the original native reader in the same window; iPad/browser use `/lan/reader` directly.
9. The final report states the iPad URL and the remaining non-final limitations.
